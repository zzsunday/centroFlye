# (c) 2020 by Authors
# This file is a part of centroFlye program.
# Released under the BSD license (see LICENSE file)


import argparse
from collections import Counter, defaultdict
import datetime
from itertools import product
import os
import sys

this_dirname = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(this_dirname, os.path.pardir))

import numpy as np
from joblib import Parallel, delayed
from tqdm import tqdm

from cloud_contig import CloudContig
from read_kmer_cloud import filter_reads_kmer_clouds
from sd_parser.sd_parser import SD_Report
from sequence_graph.db_graph import map_monoreads2scaffolds, \
    cover_scaffolds_w_reads, extract_read_pseudounits, polish
from standard_logger import get_logger


from utils.bio import RC, read_bio_seqs
from utils.git import get_git_revision_short_hash
from utils.os_utils import smart_makedirs

now = datetime.datetime.now()
date = f'{now.year}{now.month:02}{now.day:02}'
cen = 6


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sd-reads", required=True, help="SD report")
    parser.add_argument("--monomers", required=True, help="Monomers")
    parser.add_argument("--reads", required=True, help="Reads")
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--monoscaffold", required=True)
    parser.add_argument("--left-border", type=int, default=11924)
    parser.add_argument("--right-border", type=int, default=14393)
    parser.add_argument("--left-certain-border", type=int, default=11000)
    parser.add_argument("--right-certain-border", type=int, default=13000)
    parser.add_argument("--min-mult", type=int, default=3)
    parser.add_argument("--nrepeat", type=int, default=12)
    parser.add_argument("--k", type=int, default=19)
    parser.add_argument("--cloud-contig-minfreq", type=int, default=4)
    params = parser.parse_args()
    return params


def get_certain_reads(locations, monoreads, left_border, right_border):
    r_ids = []
    for r_id, locs in locations.items():
        if len(locs) != 1:
            continue
        s, e = locs[0]
        if e > left_border and s < right_border:
            r_ids.append(r_id)
    r_ids.sort(key=lambda r_id: len(monoreads[r_id]),
               reverse=True)
    return r_ids


def get_assert_monostrings_validity(monoreads):
    for r_id, monoread in monoreads.items():
        for c, minst in zip(monoread.raw_monostring, monoread.monoinstances):
            if minst.is_reliable():
                if minst.is_lowercase():
                    assert c - monoread.monomer_db.get_size() == \
                        minst.get_monoindex()
                else:
                    assert c == minst.get_monoindex()


class ReadKMerCloud:
    def __init__(self, kmers, r_id):
        self.r_id = r_id
        self.kmers = kmers
        self.all_kmers = []
        for v in self.kmers:
            self.all_kmers += v

    @classmethod
    def from_monoread(cls, monoread, k, max_mult=2):
        all_kmers = []
        all_kmer_cnt = Counter()
        for minst in monoread.monoinstances:
            nucl_segment = minst.nucl_segment
            kmer_cnt = Counter(nucl_segment[i:i+k]
                               for i in range(len(nucl_segment)-k+1))
            kmers = {kmer for kmer, cnt in kmer_cnt.items()
                     if cnt == 1}
            for kmer in kmers:
                all_kmer_cnt[kmer] += 1
            all_kmers.append(kmers)

        for i in range(len(all_kmers)):
            all_kmers[i] = {kmer for kmer in all_kmers[i]
                            if all_kmer_cnt[kmer] < max_mult}
        return cls(kmers=all_kmers, r_id=monoread.seq_id)


def monostrings2kmerclouds(monoreads, k):
    kmer_clouds = {}
    for r_id, monoread in monoreads.items():
        kmer_clouds[r_id] = ReadKMerCloud.from_monoread(monoread, k=k)
    return kmer_clouds


class SpecialCloudContig(CloudContig):
    def get_score(self, max_freq=0):
        score = 0
        for kmer in self.freq_kmers:
            cnt = 0
            for pos in self.kmer_positions[kmer]:
                if self.clouds[pos][kmer] > max_freq:
                    cnt += 1
            if cnt == 1:
                score += 1
        print(score)
        return score


def var_assess_quality(variant, q_ids, certain_rids, all_read_kmer_clouds,
                       locations, cloud_contig_minfreq):
    cloud_contig = SpecialCloudContig(cloud_contig_minfreq)
    for r_id in certain_rids:
        cloud_contig.add_read(all_read_kmer_clouds[r_id],
                              position=locations[r_id][0][0])
    base_score = cloud_contig.get_score()
    for i, q_id in enumerate(q_ids[:len(variant)]):
        loc = locations[q_id][variant[i]][0]
        cloud_contig.add_read(all_read_kmer_clouds[q_id],
                              position=loc)
    return cloud_contig.get_score() - base_score


def get_best_variant(nrepeat,
                     q_ids,
                     certain_rids,
                     all_read_kmer_clouds,
                     locations,
                     cloud_contig_minfreq,
                     logger,
                     n_threads=1):
    best_variant, best_score = None, 0
    variants = list(product([0, 1], repeat=nrepeat))
    scores = Parallel(
        n_jobs=n_threads, backend="threading")(
        delayed(var_assess_quality)
        (variant, q_ids, certain_rids, all_read_kmer_clouds, locations,
         cloud_contig_minfreq)
        for variant in tqdm(variants))

    logger.info(f'Scores {scores}')
    amax = np.argmax(scores)
    return variants[amax]


def main():
    params = parse_args()
    smart_makedirs(params.outdir)
    logfn = os.path.join(params.outdir, 'cen6_duplication.log')
    logger = get_logger(logfn,
                        logger_name='centroFlye: cen6 duplication')

    logger.info(f'cmd: {sys.argv}')
    logger.info(f'git hash: {get_git_revision_short_hash()}')

    logger.info('Reading SD reports')
    sd_report = SD_Report(sd_report_fn=params.sd_reads,
                          monomers_fn=params.monomers,
                          sequences_fn=params.reads,
                          mode='ont')
    logger.info('Finished reading SD reports')

    monoreads = sd_report.monostring_set.monostrings

    logger.info(f'Getting reads from {params.reads}')
    reads = read_bio_seqs(params.reads)
    reads = {r_id: read for r_id, read in reads.items() if r_id in monoreads}
    for r_id, read in reads.items():
        if monoreads[r_id].is_reversed:
            reads[r_id] = RC(reads[r_id])
    logger.info(f'Finished getting reads')

    logger.info(f'Getting raw monoassembly from {params.monoscaffold}')
    with open(params.monoscaffold) as f:
        raw_monoassembly = [int(x) if x.isdigit() else x
                            for x in f.readline().strip().split(' ')]
    logger.info(f'Finished getting raw monoassembly')

    logger.info(f'Mapping reads')
    locations = map_monoreads2scaffolds(monoreads,
                                        [raw_monoassembly],
                                        max_nloc=2)
    locations = locations[0]
    logger.info(f'Mapping reads')

    q_ids = [r_id for r_id in locations
             if len(locations[r_id]) == 2 and
             locations[r_id][0][1] >= params.left_border and
             locations[r_id][1][0] <= params.right_border]

    q_ids.sort(key=lambda r_id: len(monoreads[r_id]), reverse=True)
    logger.info('Uncertain reads around duplication')
    for q_id in q_ids:
        logger.info(f'{q_id} {locations[q_id]}')

    certain_rids = get_certain_reads(locations, monoreads,
                                     left_border=params.left_certain_border,
                                     right_border=params.right_certain_border)
    logger.info('Certain mapping reads')
    for r_id in certain_rids:
        logger.info(f'{r_id} {locations[r_id]}')

    logger.info('Monostrings -> kmer clouds')
    all_read_kmer_clouds = \
        monostrings2kmerclouds({r_id: monoreads[r_id]
                                for r_id in certain_rids + q_ids},
                               k=params.k)
    logger.info('Filter kmer clouds')
    all_read_kmer_clouds = filter_reads_kmer_clouds(all_read_kmer_clouds,
                                                    min_mult=params.min_mult)

    best_variant = get_best_variant(nrepeat=params.nrepeat,
                                    q_ids=q_ids,
                                    certain_rids=certain_rids,
                                    all_read_kmer_clouds=all_read_kmer_clouds,
                                    locations=locations,
                                    cloud_contig_minfreq=params.cloud_contig_minfreq,
                                    logger=logger)
    logger.info(f'Best variant {best_variant}')
    for v, q_id in zip(best_variant, q_ids):
        logger.info(f'{q_id} {v}')

    logger.info('Patching Locations')
    for i, q_id in enumerate(q_ids[:len(best_variant)]):
        locations[q_id] = [locations[q_id][best_variant[i]]]
        logger.info(f'{q_id} {locations[q_id]}')

    locations = {r_id: loc for r_id, loc in locations.items()
                 if len(loc) == 1}
    locations = [locations]
    covered_scaffolds = cover_scaffolds_w_reads(locations,
                                                monoreads,
                                                [raw_monoassembly])
    read_pseudounits = extract_read_pseudounits(covered_scaffolds,
                                                [raw_monoassembly],
                                                monoreads)
    logger.info('Polishing')
    polish(scaffolds=[raw_monoassembly],
           read_pseudounits=read_pseudounits,
           monomer_db=sd_report.monomer_db,
           outdir=params.outdir)


if __name__ == "__main__":
    main()