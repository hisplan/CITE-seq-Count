import time
import gzip
import sys
import os
import Levenshtein
import regex
import pybktree
import csv

from collections import Counter
from collections import defaultdict
from collections import namedtuple
from multiprocess import Pool

from itertools import islice
from numpy import int32
from scipy import sparse
from umi_tools import network
from umi_tools import umi_methods
import umi_tools.whitelist_methods as whitelist_methods


from cite_seq_count import secondsToText
from cite_seq_count import preprocessing


def find_best_match(TAG_seq, tags, maximum_distance):
    """
    Find the best match from the list of tags.

    Compares the Levenshtein distance between tags and the trimmed sequences.
    The tag and the sequence must have the same length.
    If no matches found returns 'unmapped'.
    We add 1
    Args:
        TAG_seq (string): Sequence from R2 already start trimmed
        tags (dict): A dictionary with the TAGs as keys and TAG Names as values.
        maximum_distance (int): Maximum distance given by the user.

    Returns:
        best_match (string): The TAG name that will be used for counting.
    """
    best_match = "unmapped"
    best_score = maximum_distance
    for tag in tags:
        score = Levenshtein.hamming(tag.sequence, TAG_seq[: len(tag.sequence)])
        if score == 0:
            # Best possible match
            return tag.name
        elif score <= best_score:
            best_score = score
            best_match = tag.name
            return best_match
    return best_match


def find_best_match_shift(TAG_seq, tags):
    """
    Find the best match from the list of tags with sliding window.
    Only works with exact match.

    Compares the Levenshtein distance between tags and the trimmed sequences.
    The tag and the sequence must have the same length.
    If no matches found returns 'unmapped'.
    We add 1
    Args:
        TAG_seq (string): Sequence from R2 already start trimmed
        tags (dict): A dictionary with the TAGs as keys and TAG Names as values.
        maximum_distance (int): Maximum distance given by the user.

    Returns:
        best_match (string): The TAG name that will be used for counting.
    """
    best_match = "unmapped"
    for tag in tags:
        if tag.sequence in TAG_seq:
            return tag.name
    return best_match


def map_reads(mapping_input):
    """Read through R1/R2 files and generate a islice starting at a specific index.

    It reads both Read1 and Read2 files, creating a dict based on cell barcode.

    Args:
        read1_path (string): Path to R1.fastq.gz
        read2_path (string): Path to R2.fastq.gz
        chunk_size (int): The number of lines to process 
        tags (dict): A dictionary with the TAGs + TAG Names.
        barcode_slice (slice): A slice for extracting the Barcode portion from the
            sequence.
        umi_slice (slice): A slice for extracting the UMI portion from the
            sequence.
        indexes (list): Pair of first and last index for islice
        debug (bool): Print debug messages. Default is False.
        start_trim (int): Number of bases to trim at the start.
        maximum_distance (int): Maximum distance given by the user.
        sliding_window (bool): A bool enabling a sliding window search

    Returns:
        results (dict): A dict of dict of Counters with the mapping results.
        no_match (Counter): A counter with unmapped sequences.
    """
    # Initiate values
    (filename, tags, debug, maximum_distance, sliding_window) = mapping_input
    print("Started mapping in child process {}".format(os.getpid()))
    results = {}
    no_match = Counter()
    n = 1
    t = time.time()

    # Progress info
    with open(filename, "r") as input_file:
        reads = csv.reader(input_file)
        for read in reads:
            cell_barcode = read[0]
            # This change in bytes is required by umi_tools for umi correction
            UMI = bytes(read[1], "ascii")
            read2 = read[2]
            if n % 1000000 == 0:
                print(
                    "Processed 1,000,000 reads in {}. Total "
                    "reads: {:,} in child {}".format(
                        secondsToText.secondsToText(time.time() - t), n, os.getpid()
                    )
                )
                sys.stdout.flush()
                t = time.time()

            if cell_barcode not in results:
                results[cell_barcode] = defaultdict(Counter)

            if sliding_window:
                best_match = find_best_match_shift(read2, tags)
            else:
                best_match = find_best_match(read2, tags, maximum_distance)

            results[cell_barcode][best_match][UMI] += 1

            if best_match == "unmapped":
                no_match[read2] += 1

            if debug:
                print(
                    "cell_barcode:{0}\tUMI:{1}\tTAG_seq:{2}\n"
                    "cell barcode length:{3}\tUMI length:{4}\tTAG sequence length:{5}\n"
                    "Best match is: {6}\n".format(
                        cell_barcode,
                        UMI,
                        read2,
                        len(cell_barcode),
                        len(UMI),
                        len(read2),
                        best_match,
                    )
                )
                sys.stdout.flush()
            n += 1
        print(
            "Mapping done for process {}. Processed {:,} reads".format(
                os.getpid(), n - 1
            )
        )
        sys.stdout.flush()

    return (results, no_match)


def merge_results(parallel_results):
    """Merge chunked results from parallel processing.

    Args:
        parallel_results (list): List of dict with mapping results.

    Returns:
        merged_results (dict): Results combined as a dict of dict of Counters
        umis_per_cell (Counter): Total umis per cell as a Counter
        reads_per_cell (Counter): Total reads per cell as a Counter
        merged_no_match (Counter): Unmapped tags as a Counter
    """
    merged_results = {}
    merged_no_match = Counter()
    umis_per_cell = Counter()
    reads_per_cell = Counter()
    for chunk in parallel_results:
        mapped = chunk[0]
        unmapped = chunk[1]
        for cell_barcode in mapped:
            if cell_barcode not in merged_results:
                merged_results[cell_barcode] = defaultdict(Counter)
            for TAG in mapped[cell_barcode]:
                # Test the counter. Returns false if empty
                if mapped[cell_barcode][TAG]:
                    for UMI in mapped[cell_barcode][TAG]:
                        merged_results[cell_barcode][TAG][UMI] += mapped[cell_barcode][
                            TAG
                        ][UMI]
                        umis_per_cell[cell_barcode] += len(mapped[cell_barcode][TAG])
                        reads_per_cell[cell_barcode] += mapped[cell_barcode][TAG][UMI]
        merged_no_match.update(unmapped)
    return (merged_results, umis_per_cell, reads_per_cell, merged_no_match)


def check_unmapped(no_match, total_reads, start_trim):
    """Check if the number of unmapped is higher than 99%"""
    if sum(no_match.values()) / total_reads > float(0.99):
        exit(
            """More than 99 percent of your data is unmapped.\nPlease check that your --start_trim {} parameter is correct and that your tags file is properly formatted""".format(
                start_trim
            )
        )


def correct_umis(umi_correction_input):
    """
    Corrects umi barcodes within same cell/tag groups.
    
    Args:
        final_results (dict): Dict of dict of Counters with mapping results.
        collapsing_threshold (int): Max distance between umis.
        top_cells (set): Set of cells to go through.
        max_umis (int): Maximum UMIs to consider for one cluster.
    
    Returns:
        final_results (dict): Same as input but with corrected umis.
        corrected_umis (int): How many umis have been corrected.
        aberrant_umi_count_cells (set): Set of uncorrected cells.
    """

    (final_results, collapsing_threshold, max_umis) = umi_correction_input
    print(
        "Started umi correction in child process {} working on {} cells".format(
            os.getpid(), len(final_results)
        )
    )
    corrected_umis = 0
    aberrant_cells = set()
    cells = final_results.keys()
    for cell_barcode in cells:
        for TAG in final_results[cell_barcode]:
            n_umis = len(final_results[cell_barcode][TAG])
            if n_umis > 1 and n_umis <= max_umis:
                umi_clusters = network.UMIClusterer()
                UMIclusters = umi_clusters(
                    final_results[cell_barcode][TAG], collapsing_threshold
                )
                (new_res, temp_corrected_umis) = update_umi_counts(
                    UMIclusters, final_results[cell_barcode].pop(TAG)
                )
                final_results[cell_barcode][TAG] = new_res
                corrected_umis += temp_corrected_umis
            elif n_umis > max_umis:
                aberrant_cells.add(cell_barcode)
    print("Finished correcting umis in child {}".format(os.getpid()))
    return (final_results, corrected_umis, aberrant_cells)


def update_umi_counts(UMIclusters, cell_tag_counts):
    """
    Update a dict object with umis corrected.

    Args:
        UMIclusters (list): List of lists with corrected umis
        cell_tag_counts (Counter): Counter of umis

    Returns:
        cell_tag_counts (Counter): Updated Counter of umis
        temp_corrected_umis (int): Number of corrected umis
    """
    temp_corrected_umis = 0
    for (
        umi_cluster
    ) in UMIclusters:  # This is a list with the first element the dominant barcode
        if len(umi_cluster) > 1:  # This means we got a correction
            major_umi = umi_cluster[0]
            for minor_umi in umi_cluster[1:]:
                temp_corrected_umis += 1
                temp = cell_tag_counts.pop(minor_umi)
                cell_tag_counts[major_umi] += temp
    return (cell_tag_counts, temp_corrected_umis)


def collapse_cells(true_to_false, umis_per_cell, final_results, ab_map):
    """
    Collapses cell barcodes based on the mapping true_to_false

    Args:
        true_to_false (dict): Mapping between the reference and the "mutated" barcodes.
        umis_per_cell (Counter): Counter of number of umis per cell.
        final_results (dict): Dict of dict of Counters with mapping results.
        ab_map (dict): Dict of the TAGS.

    Returns:
        umis_per_cell (Counter): Counter of number of umis per cell.
        final_results (dict): Same as input but with corrected cell barcodes.
        corrected_barcodes (int): How many cell barcodes have been corrected.
    """
    print("Collapsing cell barcodes")
    corrected_barcodes = 0
    for real_barcode in true_to_false:
        # If the cell barcode is not in the results
        if real_barcode not in final_results:
            final_results[real_barcode] = defaultdict()
            for TAG in ab_map:
                final_results[real_barcode][TAG] = Counter()
        for fake_barcode in true_to_false[real_barcode]:
            temp = final_results.pop(fake_barcode)
            corrected_barcodes += 1
            for TAG in temp.keys():
                final_results[real_barcode][TAG].update(temp[TAG])
            temp_umi_counts = umis_per_cell.pop(fake_barcode)
            # temp_read_counts = reads_per_cell.pop(fake_barcode)

            umis_per_cell[real_barcode] += temp_umi_counts
            # reads_per_cell[real_barcode] += temp_read_counts

    return (umis_per_cell, final_results, corrected_barcodes)


def correct_cells(
    final_results,
    reads_per_cell,
    umis_per_cell,
    collapsing_threshold,
    expected_cells,
    ab_map,
):
    """
    Corrects cell barcodes.
    
    Args:
        final_results (dict): Dict of dict of Counters with mapping results.
        umis_per_cell (Counter): Counter of number of umis per cell.
        collapsing_threshold (int): Max distance between umis.
        expected_cells (int): Number of expected cells.
        ab_map (dict): Dict of the TAGS.
    
    Returns:
        final_results (dict): Same as input but with corrected umis.
        umis_per_cell (Counter): Counter of umis per cell after cell barcode correction
        corrected_umis (int): How many umis have been corrected.
    """
    print("Looking for a whitelist")
    _, true_to_false = whitelist_methods.getCellWhitelist(
        cell_barcode_counts=reads_per_cell,
        expect_cells=expected_cells,
        cell_number=expected_cells,
        error_correct_threshold=collapsing_threshold,
        plotfile_prefix=False,
    )

    (umis_per_cell, final_results, corrected_barcodes) = collapse_cells(
        true_to_false=true_to_false,
        umis_per_cell=umis_per_cell,
        final_results=final_results,
        ab_map=ab_map,
    )
    return (final_results, umis_per_cell, corrected_barcodes)


def correct_cells_whitelist(
    final_results, umis_per_cell, whitelist, collapsing_threshold, ab_map
):
    """
    Corrects cell barcodes.
    
    Args:
        final_results (dict): Dict of dict of Counters with mapping results.
        umis_per_cell (Counter): Counter of UMIs per cell.
        whitelist (set): The whitelist reference given by the user.
        collapsing_threshold (int): Max distance between umis.
        ab_map (OrederedDict): Tags in an ordered dict.

    
    Returns:
        final_results (dict): Same as input but with corrected umis.
        umis_per_cell (Counter): Updated UMI counts after correction.
        corrected_barcodes (int): How many umis have been corrected.
    """
    barcode_tree = pybktree.BKTree(Levenshtein.hamming, whitelist)
    print("Generated barcode tree from whitelist")
    cell_barcodes = list(final_results.keys())
    n_barcodes = len(cell_barcodes)
    print("Finding reference candidates")
    print("Processing {:,} cell barcodes".format(n_barcodes))

    # Run with one process
    true_to_false = find_true_to_false_map(
        barcode_tree=barcode_tree,
        cell_barcodes=cell_barcodes,
        whitelist=whitelist,
        collapsing_threshold=collapsing_threshold,
    )
    (umis_per_cell, final_results, corrected_barcodes) = collapse_cells(
        true_to_false, umis_per_cell, final_results, ab_map
    )
    return (final_results, umis_per_cell, corrected_barcodes)


def find_true_to_false_map(
    barcode_tree, cell_barcodes, whitelist, collapsing_threshold
):
    """
    Creates a mapping between "fake" cell barcodes and their original true barcode.

    Args:
        barcode_tree (BKTree): BKTree of all original cell barcodes.
        cell_barcodes (List): Cell barcodes to go through.
        whitelist (Set): Set of the whitelist, the "true" cell barcodes.
        collasping_threshold (int): How many mistakes to correct.

    Return:
        true_to_false (defaultdict(list)): Contains the mapping between the fake and real barcodes. The key is the real one.
    """
    true_to_false = defaultdict(list)
    for cell_barcode in cell_barcodes:
        if cell_barcode in whitelist:
            # if the barcode is already whitelisted, no need to add
            continue
        # get all members of whitelist that are at distance of collapsing_threshold
        candidates = [
            white_cell
            for d, white_cell in barcode_tree.find(cell_barcode, collapsing_threshold)
            if d > 0
        ]
        if len(candidates) == 1:
            white_cell_str = candidates[0]
            true_to_false[white_cell_str].append(cell_barcode)
        elif len(candidates) == 0:
            # the cell doesnt match to any whitelisted barcode,
            # hence we have to drop it
            # (as it cannot be asscociated with any frequent barcode)
            continue
        else:
            # more than on whitelisted candidate:
            # we drop it as its not uniquely assignable
            continue
    return true_to_false


def generate_sparse_matrices(final_results, ordered_tags_map, top_cells):
    """
    Create two sparse matrices with umi and read counts.

    Args:
        final_results (dict): Results in a dict of dicts of Counters.
        ordered_tags_map (dict): Tags in order with indexes as values.

    Returns:
        umi_results_matrix (scipy.sparse.dok_matrix): UMI counts
        read_results_matrix (scipy.sparse.dok_matrix): Read counts

    """
    umi_results_matrix = sparse.dok_matrix(
        (len(ordered_tags_map), len(top_cells)), dtype=int32
    )
    read_results_matrix = sparse.dok_matrix(
        (len(ordered_tags_map), len(top_cells)), dtype=int32
    )
    for i, cell_barcode in enumerate(top_cells):
        for j, TAG in enumerate(final_results[cell_barcode]):
            if final_results[cell_barcode][TAG]:
                umi_results_matrix[ordered_tags_map[TAG]["id"], i] = len(
                    final_results[cell_barcode][TAG]
                )
                read_results_matrix[ordered_tags_map[TAG]["id"], i] = sum(
                    final_results[cell_barcode][TAG].values()
                )
    return (umi_results_matrix, read_results_matrix)

