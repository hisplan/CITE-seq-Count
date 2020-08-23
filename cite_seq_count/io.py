import os
import gzip
import shutil
import time
import datetime

from collections import namedtuple
from itertools import islice

import pandas as pd

from scipy import io
from cite_seq_count import secondsToText


def write_to_files(sparse_matrix, top_cells, ordered_tags_map, data_type, outfolder):
    """Write the umi and read sparse matrices to file in gzipped mtx format.

    Args:
        sparse_matrix (dok_matrix): Results in a sparse matrix.
        top_cells (set): Set of cells that are selected for output.
        ordered_tags_map (dict): Tags in order with indexes as values.
        data_type (string): A string definning if the data is umi or read based.
        outfolder (string): Path to the output folder.
    """
    prefix = os.path.join(outfolder, data_type + "_count")
    os.makedirs(prefix, exist_ok=True)
    io.mmwrite(os.path.join(prefix, "matrix.mtx"), sparse_matrix)
    with gzip.open(os.path.join(prefix, "barcodes.tsv.gz"), "wb") as barcode_file:
        for barcode in top_cells:
            barcode_file.write("{}\n".format(barcode).encode())
    with gzip.open(os.path.join(prefix, "features.tsv.gz"), "wb") as feature_file:
        for feature in ordered_tags_map:
            feature_file.write(
                "{}\t{}\n".format(
                    ordered_tags_map[feature]["sequence"], feature
                ).encode()
            )
    with open(os.path.join(prefix, "matrix.mtx"), "rb") as mtx_in:
        with gzip.open(os.path.join(prefix, "matrix.mtx") + ".gz", "wb") as mtx_gz:
            shutil.copyfileobj(mtx_in, mtx_gz)
    os.remove(os.path.join(prefix, "matrix.mtx"))


def write_dense(sparse_matrix, index, columns, outfolder, filename):
    """
    Writes a dense matrix in a csv format
    
    Args:
       sparse_matrix (dok_matrix): Results in a sparse matrix.
       index (list): List of TAGS
       columns (set): List of cells
       outfolder (str): Output folder
       filename (str): Filename
    """
    prefix = os.path.join(outfolder)
    os.makedirs(prefix, exist_ok=True)
    pandas_dense = pd.DataFrame(sparse_matrix.todense(), columns=columns, index=index)
    pandas_dense.to_csv(os.path.join(outfolder, filename), sep="\t")


def write_unmapped(merged_no_match, top_unknowns, outfolder, filename):
    """
    Writes a list of top unmapped sequences

    Args:
        merged_no_match (Counter): Counter of unmapped sequences
        top_unknowns (int): Number of unmapped sequences to output
        outfolder (string): Path of the output folder
        filename (string): Name of the output file
    """

    top_unmapped = merged_no_match.most_common(top_unknowns)

    with open(os.path.join(outfolder, filename), "w") as unknown_file:
        unknown_file.write("tag,count\n")
        for element in top_unmapped:
            unknown_file.write("{},{}\n".format(element[0], element[1]))


def create_report(
    total_reads,
    reads_per_cell,
    no_match,
    version,
    start_time,
    ordered_tags_map,
    umis_corrected,
    bcs_corrected,
    bad_cells,
    R1_too_short,
    R2_too_short,
    args,
    chemistry_def,
):
    """
    Creates a report with details about the run in a yaml format.
    Args:
        total_reads (int): Number of reads that have been processed.
        reads_matrix (scipy.sparse.dok_matrix): A sparse matrix continining read counts.
        no_match (Counter): Counter of unmapped tags.
        version (string): CITE-seq-Count package version.
        start_time (time): Start time of the run.
        args (arg_parse): Arguments provided by the user.

    """
    total_unmapped = sum(no_match.values())
    total_mapped = total_reads - total_unmapped
    total_too_short = total_reads - total_unmapped - total_mapped
    too_short_perc = round((total_too_short / total_reads) * 100)
    mapped_perc = round((total_mapped / total_reads) * 100)
    unmapped_perc = round((total_unmapped / total_reads) * 100)

    with open(os.path.join(args.outfolder, "run_report.yaml"), "w") as report_file:
        report_file.write(
            """Date: {}
Running time: {}
CITE-seq-Count Version: {}
Reads processed: {}
Percentage mapped: {}
Percentage unmapped: {}
Percentage too short: {}
\tR1_too_short: {}
\tR2_too_short: {}
Uncorrected cells: {}
Correction:
\tCell barcodes collapsing threshold: {}
\tCell barcodes corrected: {}
\tUMI collapsing threshold: {}
\tUMIs corrected: {}
Run parameters:
\tRead1_paths: {}
\tRead2_paths: {}
\tCell barcode:
\t\tFirst position: {}
\t\tLast position: {}
\tUMI barcode:
\t\tFirst position: {}
\t\tLast position: {}
\tExpected cells: {}
\tTags max errors: {}
\tStart trim: {}
""".format(
                datetime.datetime.today().strftime("%Y-%m-%d"),
                secondsToText.secondsToText(time.time() - start_time),
                version,
                int(total_reads),
                mapped_perc,
                unmapped_perc,
                too_short_perc,
                R1_too_short,
                R2_too_short,
                len(bad_cells),
                args.bc_threshold,
                bcs_corrected,
                args.umi_threshold,
                umis_corrected,
                args.read1_path,
                args.read2_path,
                args.cb_first,
                args.cb_last,
                args.umi_first,
                chemistry_def.umi_barcode_end,
                args.expected_cells,
                args.max_error,
                chemistry_def.R2_trim_start,
            )
        )


def write_chunks_to_disk(
    args,
    read1_paths,
    read2_paths,
    R2_max_length,
    total_reads,
    chemistry_def,
    named_tuples_tags_map,
):
    """
    """
    mapping_input = namedtuple(
        "mapping_input",
        ["filename", "tags", "debug", "maximum_distance", "sliding_window"],
    )

    print("Writing chunks to disk")

    num_chunk = 0
    if not args.chunk_size:
        args.chunk_size = round(total_reads / args.n_threads) + 1
    temp_path = os.path.abspath(args.temp_path)
    input_queue = []
    temp_files = []
    R1_too_short = 0
    R2_too_short = 0
    total_reads_written = 0

    barcode_slice = slice(
        chemistry_def.cell_barcode_start - 1, chemistry_def.cell_barcode_end
    )
    umi_slice = slice(
        chemistry_def.umi_barcode_start - 1, chemistry_def.umi_barcode_end
    )

    for read1_path, read2_path in zip(read1_paths, read2_paths):
        print("Reading reads from files: {}, {}".format(read1_path, read2_path))
        with gzip.open(read1_path, "rt") as textfile1, gzip.open(
            read2_path, "rt"
        ) as textfile2:
            secondlines = islice(zip(textfile1, textfile2), 1, None, 4)
            temp_filename = os.path.join(temp_path, "temp_{}".format(num_chunk))
            chunked_file_object = open(temp_filename, "w")
            temp_files.append(os.path.abspath(temp_filename))
            reads_written = 0
            for read1, read2 in secondlines:

                read1 = read1.strip()
                if len(read1) < chemistry_def.umi_barcode_end:
                    R1_too_short += 1
                    # The entire read is skipped
                    continue
                if len(read2) < R2_max_length:
                    R2_too_short += 1
                    # The entire read is skipped
                    continue

                read1_sliced = read1[
                    chemistry_def.cell_barcode_start - 1 : chemistry_def.umi_barcode_end
                ]

                read2_sliced = read2[
                    chemistry_def.R2_trim_start : (
                        R2_max_length + chemistry_def.R2_trim_start
                    )
                ]
                chunked_file_object.write(
                    "{},{},{}\n".format(
                        read1_sliced[barcode_slice],
                        read1_sliced[umi_slice],
                        read2_sliced,
                    )
                )

                reads_written += 1
                total_reads_written += 1
                if reads_written % args.chunk_size == 0:
                    input_queue.append(
                        mapping_input(
                            filename=temp_filename,
                            tags=named_tuples_tags_map,
                            debug=args.debug,
                            maximum_distance=args.max_error,
                            sliding_window=args.sliding_window,
                        )
                    )
                    num_chunk += 1
                    chunked_file_object.close()
                    temp_filename = "temp_{}".format(num_chunk)
                    chunked_file_object = open(temp_filename, "w")
                    temp_files.append(os.path.abspath(temp_filename))
                    reads_written = 0
                if total_reads_written >= args.first_n:
                    total_reads = total_reads_written
                    break

            input_queue.append(
                mapping_input(
                    filename=temp_filename,
                    tags=named_tuples_tags_map,
                    debug=args.debug,
                    maximum_distance=args.max_error,
                    sliding_window=args.sliding_window,
                )
            )
            chunked_file_object.close()
    return input_queue, temp_files, R1_too_short, R2_too_short, total_reads
