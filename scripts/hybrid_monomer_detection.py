import argparse
from collections import Counter
from pprint import pprint

from joblib import Parallel, delayed
import numpy as np
from statsmodels.stats.proportion import proportions_ztest

from sd_parser import SD_Report
from utils.bio import read_bio_seqs, hybrid_alignment, RC, \
                      calc_identity, write_bio_seqs


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sd", required=True, help="SD report")
    parser.add_argument("--monomers", required=True, help="Monomers")
    parser.add_argument("--reads", required=True, help="Cen reads")
    parser.add_argument("--max-ident-diff", type=int, default=5)
    parser.add_argument("--min-sec-ident", type=int, default=80)
    parser.add_argument("--threads", type=int, default=50)
    parser.add_argument("--outfile", required=True)
    parser.add_argument("--genome-len", type=int, required=True)
    params = parser.parse_args()
    return params


def read_input(params):
    sd_report = SD_Report(SD_report_fn=params.sd,
                          monomers_fn=params.monomers,
                          ident_hybrid=False)
    reads = read_bio_seqs(params.reads)
    return sd_report, reads


def get_putative_pairs(names):
    putative_pairs = []
    for m1 in names:
        for m2 in names:
            if m1.islower() or m2.islower() or (m1 >= m2):
                continue
            else:
                putative_pairs.append((m1, m2))
    return putative_pairs


def extract_hybrid(pair, df, reads,
                   A, B,
                   max_ident_diff,
                   min_sec_ident,
                   threads,
                   coverage,
                   sign_lev=0.05):
    def extract_read_segms(df, reads):
        segms = []
        for i, row in df.iterrows():
            r_id, st, en = row.r_id, row.r_st, row.r_en + 1
            monomer = row.monomer
            segm = reads[r_id][st:en]
            if monomer.islower():
                segm = RC(segm)
            segms.append(segm)
        return segms

    def get_cuts(segms, A, B, t=threads):
        results = \
            Parallel(n_jobs=t)(delayed(hybrid_alignment)(segm, A, B)
                               for segm in segms)
        jumps = []
        for _, _, max_sc, sec_max_sc, jump, orient in results:
            if max_sc >= sec_max_sc * 1.1:
                jumps.append((jump[1:], orient))
        return jumps

    def n_improved_pairs(segms, A, B, hybrid):
        n_improved = 0
        min_improved, max_improved = 1, 0
        for segm in segms:
            identA = calc_identity(segm, A)
            identB = calc_identity(segm, B)
            ident_hybrid = calc_identity(segm, hybrid)
            if ident_hybrid > max(identA, identB):
                n_improved += 1
                min_improved = min(min_improved, ident_hybrid)
                max_improved = max(max_improved, ident_hybrid)
        return n_improved, min_improved, max_improved

    results = {
        "status": False,
    }
    bases = list(pair)
    bases += [b.lower() for b in bases]
    AB = np.where(df.monomer.isin(bases) &
                  df.sec_monomer.isin(bases) &
                  (abs(df.identity - df.sec_identity) < max_ident_diff) &
                  (df.sec_identity > min_sec_ident) &
                  df.reliability.isin(['+']))[0]
    dfAB = df.iloc[AB]

    read_segms = extract_read_segms(dfAB, reads)
    np.random.shuffle(read_segms)
    # print(len(read_segms))
    if len(read_segms) < coverage * 0.5:
        return results

    nreads = min(2000, len(read_segms)) // 2

    results["ntrain"] = nreads
    results["ntest"] = nreads
    train, test = read_segms[:nreads], read_segms[nreads:nreads*2]
    jumps = Counter(get_cuts(train, A, B))
    if len(jumps) == 0:
        return results
    results["jumps"] = jumps

    # print(jumps)
    mc_jump = jumps.most_common(1)[0]
    if mc_jump[1] < 5:
        return results
    results["mc_jump"] = mc_jump[0]
    mc_jump, orient = mc_jump[0]

    if orient == '>':
        hybrid = A[:mc_jump[0]] + B[mc_jump[1]:]
    else:
        hybrid = B[:mc_jump[0]] + A[mc_jump[1]:]

    train_n_improved, min_train_ident, max_train_ident = \
        n_improved_pairs(train, A, B, hybrid)
    test_n_improved, min_test_ident, max_test_ident = \
        n_improved_pairs(test, A, B, hybrid)
    min_ident = min(min_train_ident, min_test_ident)
    max_ident = max(max_train_ident, max_test_ident)
    results["min_ident"] = min_ident
    results["max_ident"] = max_ident

    # print(train_n_improved, test_n_improved, len(train), len(test))
    if train_n_improved == 0 or test_n_improved == 0:
        return results
    elif train_n_improved == len(train) or test_n_improved == len(test):
        results["train_n_improved"] = train_n_improved
        results["test_n_improved"] = test_n_improved
        results["hybrid"] = hybrid
        results["status"] = True
        return results  # (hybrid, (mc_jump, orient))

    stat, pval = proportions_ztest([train_n_improved, test_n_improved],
                                   [len(train), len(test)])
    results["stat"] = stat
    results["pval"] = pval
    results["train_n_improved"] = train_n_improved
    results["test_n_improved"] = test_n_improved
    results["hybrid"] = hybrid
    results["status"] = True
    return results  # (hybrid, (mc_jump, orient))


def main():
    params = parse_args()
    sd_report, reads = read_input(params)
    coverage = 171 * len(sd_report.df) / params.genome_len
    print(f'Estimated coverage {coverage}x')

    putative_pairs = get_putative_pairs(sd_report.monomer_names_map.values())
    # putative_pairs = [('K', 'L')]
    # print(putative_pairs)

    hybrids = {}
    for pair in putative_pairs:
        A = sd_report.monomers[sd_report.rev_monomer_names_map[pair[0]]]
        B = sd_report.monomers[sd_report.rev_monomer_names_map[pair[1]]]
        print(pair)
        extraction_res = extract_hybrid(pair, sd_report.df, reads,
                                        A, B,
                                        params.max_ident_diff,
                                        params.min_sec_ident,
                                        params.threads,
                                        coverage)
        if extraction_res["status"]:
            pprint(extraction_res, width=1)
            print("")
            hybrid, (jump, orient) = extraction_res["hybrid"], extraction_res["mc_jump"]
            hybrids[f'{pair[0]}_{pair[1]}|{jump[0]}_{jump[1]}|{orient}'] = hybrid
    write_bio_seqs(params.outfile, hybrids)


if __name__ == "__main__":
    main()
