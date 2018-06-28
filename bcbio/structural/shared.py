"""Shared functionality useful across multiple structural variant callers.

Handles exclusion regions and preparing discordant regions.
"""
import collections
import os

import numpy
import pybedtools
import pysam
import toolz as tz
import yaml

from bcbio import bam, utils
from bcbio.distributed.transaction import file_transaction
from bcbio.bam import callable
from bcbio.pipeline import datadict as dd
from bcbio.pipeline import shared
from bcbio.provenance import do
from bcbio.variation import effects, population, vcfutils
from functools import reduce

# ## Finalizing samples

def finalize_sv(orig_vcf, data, items):
    """Finalize structural variants, adding effects and splitting if needed.
    """
    paired = vcfutils.get_paired(items)
    # For paired/somatic, attach combined calls to tumor sample
    if paired:
        sample_vcf = orig_vcf if paired.tumor_name == dd.get_sample_name(data) else None
    else:
        sample_vcf = "%s-%s.vcf.gz" % (utils.splitext_plus(orig_vcf)[0], dd.get_sample_name(data))
        sample_vcf = vcfutils.select_sample(orig_vcf, dd.get_sample_name(data), sample_vcf, data["config"])
    if sample_vcf:
        effects_vcf, _ = effects.add_to_vcf(sample_vcf, data, "snpeff")
    else:
        effects_vcf = None
    return effects_vcf or sample_vcf

# ## Case/control

def find_case_control(items):
    """Find case/control items in a population of multiple samples.
    """
    cases = []
    controls = []
    for data in items:
        if population.get_affected_status(data) == 1:
            controls.append(data)
        else:
            cases.append(data)
    return cases, controls

# ## Prepare exclusion regions (repeats, telomeres, centromeres)

def _get_sv_exclude_file(items):
    """Retrieve SV file of regions to exclude.
    """
    sv_bed = utils.get_in(items[0], ("genome_resources", "variation", "sv_repeat"))
    if sv_bed and os.path.exists(sv_bed):
        return sv_bed

def _get_variant_regions(items):
    """Retrieve variant regions defined in any of the input items.
    """
    return [x for x in [tz.get_in(("config", "algorithm", "variant_regions"), data)
                   for data in items
                   if tz.get_in(["config", "algorithm", "coverage_interval"], data) != "genome"] if x is not None]

def has_variant_regions(items, base_file, chrom=None):
    """Determine if we should process this chromosome: needs variant regions defined.
    """
    if chrom:
        all_vrs = _get_variant_regions(items)
        if len(all_vrs) > 0:
            test = shared.subset_variant_regions(tz.first(all_vrs), chrom, base_file, items)
            if test == chrom:
                return False
    return True

def prepare_exclude_file(items, base_file, chrom=None):
    """Prepare a BED file for exclusion.

    Excludes high depth and centromere regions which contribute to long run times and
    false positive structural variant calls.
    """
    items = shared.add_highdepth_genome_exclusion(items)
    out_file = "%s-exclude%s.bed" % (utils.splitext_plus(base_file)[0], "-%s" % chrom if chrom else "")
    if not utils.file_exists(out_file) and not utils.file_exists(out_file + ".gz"):
        with shared.bedtools_tmpdir(items[0]):
            with file_transaction(items[0], out_file) as tx_out_file:
                # Get a bedtool for the full region if no variant regions
                want_bedtool = callable.get_ref_bedtool(tz.get_in(["reference", "fasta", "base"], items[0]),
                                                        items[0]["config"], chrom)
                want_bedtool = pybedtools.BedTool(shared.subset_variant_regions(want_bedtool.saveas().fn,
                                                                                chrom, tx_out_file, items))
                sv_exclude_bed = _get_sv_exclude_file(items)
                if sv_exclude_bed and len(want_bedtool) > 0:
                    want_bedtool = want_bedtool.subtract(sv_exclude_bed, nonamecheck=True).saveas()
                full_bedtool = callable.get_ref_bedtool(tz.get_in(["reference", "fasta", "base"], items[0]),
                                                        items[0]["config"])
                if len(want_bedtool) > 0:
                    full_bedtool.subtract(want_bedtool, nonamecheck=True).saveas(tx_out_file)
                else:
                    full_bedtool.saveas(tx_out_file)
    return out_file

def exclude_by_ends(in_file, exclude_file, data, in_params=None):
    """Exclude calls based on overlap of the ends with exclusion regions.

    Removes structural variants with either end being in a repeat: a large
    source of false positives.

    Parameters tuned based on removal of LCR overlapping false positives in DREAM
    synthetic 3 data.
    """
    params = {"end_buffer": 50,
              "rpt_pct": 0.9,
              "total_rpt_pct": 0.2,
              "sv_pct": 0.5}
    if in_params:
        params.update(in_params)
    assert in_file.endswith(".bed")
    out_file = "%s-norepeats%s" % utils.splitext_plus(in_file)
    to_filter = collections.defaultdict(list)
    removed = 0
    if not utils.file_uptodate(out_file, in_file):
        with file_transaction(data, out_file) as tx_out_file:
            with shared.bedtools_tmpdir(data):
                for coord, end_name in [(1, "end1"), (2, "end2")]:
                    base, ext = utils.splitext_plus(tx_out_file)
                    end_file = _create_end_file(in_file, coord, params, "%s-%s%s" % (base, end_name, ext))
                    to_filter = _find_to_filter(end_file, exclude_file, params, to_filter)
            with open(tx_out_file, "w") as out_handle:
                with open(in_file) as in_handle:
                    for line in in_handle:
                        key = "%s:%s-%s" % tuple(line.strip().split("\t")[:3])
                        total_rpt_size = sum(to_filter.get(key, [0]))
                        if total_rpt_size <= (params["total_rpt_pct"] * params["end_buffer"]):
                            out_handle.write(line)
                        else:
                            removed += 1
    return out_file, removed

def _find_to_filter(in_file, exclude_file, params, to_exclude):
    """Identify regions in the end file that overlap the exclusion file.

    We look for ends with a large percentage in a repeat or where the end contains
    an entire repeat.
    """
    for feat in pybedtools.BedTool(in_file).intersect(pybedtools.BedTool(exclude_file), wao=True, nonamecheck=True):
        us_chrom, us_start, us_end, name, other_chrom, other_start, other_end, overlap = feat.fields
        if float(overlap) > 0:
            other_size = float(other_end) - float(other_start)
            other_pct = float(overlap) / other_size
            us_pct = float(overlap) / (float(us_end) - float(us_start))
            if us_pct > params["sv_pct"] or (other_pct > params["rpt_pct"]):
                to_exclude[name].append(float(overlap))
    return to_exclude

def _create_end_file(in_file, coord, params, out_file):
    with open(in_file) as in_handle:
        with open(out_file, "w") as out_handle:
            for line in in_handle:
                parts = line.strip().split("\t")
                name = "%s:%s-%s" % tuple(parts[:3])
                curpos = int(parts[coord])
                if coord == 1:
                    start, end = curpos, curpos + params["end_buffer"]
                else:
                    start, end = curpos - params["end_buffer"], curpos
                if start > 0:
                    out_handle.write("\t".join([parts[0], str(start),
                                                str(end), name])
                                     + "\n")
    return out_file

def get_sv_chroms(items, exclude_file):
    """Retrieve chromosomes to process on, avoiding extra skipped chromosomes.
    """
    exclude_regions = {}
    for region in pybedtools.BedTool(exclude_file):
        if int(region.start) == 0:
            exclude_regions[region.chrom] = int(region.end)
    out = []
    with pysam.Samfile(items[0]["work_bam"], "rb") as pysam_work_bam:
        for chrom, length in zip(pysam_work_bam.references, pysam_work_bam.lengths):
            exclude_length = exclude_regions.get(chrom, 0)
            if exclude_length < length:
                out.append(chrom)
    return out

# ## Read preparation

def _extract_split_and_discordants(in_bam, work_dir, data):
    """Retrieve split-read alignments from input BAM file.
    """
    sr_file = os.path.join(work_dir, "%s-sr.bam" % os.path.splitext(os.path.basename(in_bam))[0])
    disc_file = os.path.join(work_dir, "%s-disc.bam" % os.path.splitext(os.path.basename(in_bam))[0])
    if not utils.file_exists(sr_file) or not utils.file_exists(disc_file):
        with file_transaction(data, sr_file) as tx_sr_file:
            with file_transaction(data, disc_file) as tx_disc_file:
                cores = dd.get_num_cores(data)
                ref_file = dd.get_ref_file(data)
                cmd = ("extract-sv-reads -e --threads {cores} -T {ref_file} "
                       "-i {in_bam} -s {tx_sr_file} -d {tx_disc_file}")
                do.run(cmd.format(**locals()), "extract split and discordant reads", data)
    for fname in [sr_file, disc_file]:
        bam.index(fname, data["config"])
    return sr_file, disc_file

def find_existing_split_discordants(data):
    """Check for pre-calculated split reads and discordants done as part of alignment streaming.
    """
    in_bam = dd.get_align_bam(data)
    sr_file = "%s-sr.bam" % os.path.splitext(in_bam)[0]
    disc_file = "%s-disc.bam" % os.path.splitext(in_bam)[0]
    if utils.file_exists(sr_file) and utils.file_exists(disc_file):
        return sr_file, disc_file
    else:
        sr_file = dd.get_sr_bam(data)
        disc_file = dd.get_disc_bam(data)
        if sr_file and utils.file_exists(sr_file) and disc_file and utils.file_exists(disc_file):
            return sr_file, disc_file
        else:
            return None, None

def get_split_discordants(data, work_dir):
    """Retrieve split and discordant reads, potentially calculating with extract_sv_reads as needed.
    """
    align_bam = dd.get_align_bam(data)
    sr_bam, disc_bam = find_existing_split_discordants(data)
    if not sr_bam:
        work_dir = (work_dir if not os.access(os.path.dirname(align_bam), os.W_OK | os.X_OK)
                    else os.path.dirname(align_bam))
        sr_bam, disc_bam = _extract_split_and_discordants(align_bam, work_dir, data)
    return sr_bam, disc_bam

def get_cur_batch(items):
    """Retrieve name of the batch shared between all items in a group.
    """
    batches = []
    for data in items:
        batch = tz.get_in(["metadata", "batch"], data, [])
        batches.append(set(batch) if isinstance(batch, (list, tuple)) else set([batch]))
    combo_batches = reduce(lambda b1, b2: b1.intersection(b2), batches)
    if len(combo_batches) == 1:
        return combo_batches.pop()
    elif len(combo_batches) == 0:
        return None
    else:
        raise ValueError("Found multiple overlapping batches: %s -- %s" % (combo_batches, batches))

def outname_from_inputs(in_files):
    base = os.path.commonprefix(in_files)
    if base.endswith("chr"):
        base = base[:-3]
    while base.endswith(("-", "_", ".")):
        base = base[:-1]
    return base

# -- Insert size calculation

def insert_size_stats(dists):
    """Calcualtes mean/median and MAD from distances, avoiding outliers.

    MAD is the Median Absolute Deviation: http://en.wikipedia.org/wiki/Median_absolute_deviation
    """
    med = numpy.median(dists)
    filter_dists = [x for x in dists if x < med + 10 * med]
    median = numpy.median(filter_dists)
    return {"mean": float(numpy.mean(filter_dists)), "std": float(numpy.std(filter_dists)),
            "median": float(median),
            "mad": float(numpy.median([abs(x - median) for x in filter_dists]))}

def calc_paired_insert_stats(in_bam, nsample=1000000):
    """Retrieve statistics for paired end read insert distances.
    """
    dists = []
    n = 0
    with pysam.Samfile(in_bam, "rb") as in_pysam:
        for read in in_pysam:
            if read.is_proper_pair and read.is_read1:
                n += 1
                dists.append(abs(read.isize))
                if n >= nsample:
                    break
    return insert_size_stats(dists)

def calc_paired_insert_stats_save(in_bam, stat_file, nsample=1000000):
    """Calculate paired stats, saving to a file for re-runs.
    """
    if utils.file_exists(stat_file):
        with open(stat_file) as in_handle:
            return yaml.safe_load(in_handle)
    else:
        stats = calc_paired_insert_stats(in_bam, nsample)
        with open(stat_file, "w") as out_handle:
            yaml.safe_dump(stats, out_handle, default_flow_style=False, allow_unicode=False)
        return stats
