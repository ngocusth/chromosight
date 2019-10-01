from __future__ import absolute_import
import sys
import numpy as np
from scipy.sparse import lil_matrix, coo_matrix, csr_matrix, triu, csc_matrix
from scipy.sparse.csgraph import connected_components
import warnings

warnings.filterwarnings("ignore")


def validate_patterns(
    coords, matrix, conv_mat, detectable_bins, kernel_matrix, max_undetected_perc
):
    # Pre-compute height, width and half (radius)
    win_h, win_w = kernel_matrix.shape
    half_h, half_w = win_h // 2 + 1, win_w // 2 + 1
    # Store coords to drop
    blacklist = []
    detectable_rows = set(detectable_bins[0])
    detectable_cols = set(detectable_bins[1])
    # Copy coords object and append column for scores
    validated_coords = np.append(coords, np.zeros((coords.shape[0], 1)), 1)
    # Initialize structure to store pattern windows
    pattern_windows = np.zeros(
        (win_h, win_w, coords.shape[0])
    )  # list containing all pannel of detected patterns
    for i, l in enumerate(coords):
        p1 = int(l[0])
        p2 = int(l[1])
        if p1 > p2:
            p1, p2 = p2, p1
        # Check for out of bounds errors
        if (
            p1 - half_h >= 0
            and p1 + half_h + 1 < matrix.shape[0]
            and p2 - half_w >= 0
            and p2 + half_w + 1 < matrix.shape[1]
        ):
            # Get bin ids to use in window
            win_rows, win_cols = (
                range(p1 - half_h + 1, p1 + half_h),
                range(p2 - half_w + 1, p2 + half_w),
            )
            # Subset window from chrom matrix
            pattern_window = matrix[np.ix_(win_rows, win_cols)].todense()
            n_rows, n_cols = pattern_window.shape
            tot_pixels = n_rows * n_cols
            # Compute number of missing rows and cols
            n_bad_rows = n_rows - len(set(win_rows).intersection(detectable_rows))
            n_bad_cols = n_cols - len(set(win_cols).intersection(detectable_cols))

            # Number of undetected pixels is "bad rows area" + "bad cols area" - "bad rows x bad cols intersection"
            tot_undetected = (
                n_bad_rows * n_cols + n_bad_cols * n_rows - n_bad_rows * n_bad_cols
            )
            # The pattern should not contain more undetectable bins that the max
            # value defined in kernel config
            if tot_undetected / tot_pixels < max_undetected_perc / 100.0:

                validated_coords[i, 2] = conv_mat[l[0], l[1]]
                pattern_windows[:, :, i] = pattern_window
            else:
                # Current pattern will be dropped due to undetectable bins
                blacklist.append(i)
        # if len(pattern_windows) > 0:
        # from matplotlib import pyplot as plt
        # fig, ax = plt.subplots(len(pattern_windows), 1)
        # for i, axi in enumerate(ax.flatten()):
        #         axi.imshow(pattern_windows[i])
        # plt.show()
        else:
            # Current pattern will be dropped due to out of bound error
            blacklist.append(i)

    # Drop patterns that did not pass filters
    blacklist = np.array(blacklist)
    filtered_coords = validated_coords[~blacklist, :]
    filtered_windows = pattern_windows[:, :, ~blacklist]

    return filtered_coords, filtered_windows


def pileup_patterns(pattern_windows):
    """Generate a pileup from an input list of pattern coords and a Hi-C matrix"""
    return np.apply_along_axis(np.median, 2, pattern_windows)


def pattern_detector(contact_map, kernel_config, kernel_matrix, area=8):
    """Pattern detector

    Detect patterns by iterated kernel matching, and compute the resulting
    'pileup pattern' as matched on the matrices.

    Parameters
    ----------
    contact_map : ContactMap object
        An object containing a Hi-C contact map with chromosome indices, 
        chromosome names,  inter and intra chromosome sub-matrices and other
        attributes.
    kernel_config : dict
        The kernel configuration, as documented in
        chromovision.utils.io.load_kernel_config
    kernel_matrix : numpy.array
        The kernel matrix to use for convolution as a 2D numpy array
    area : int, optional
        The window size of the pileup pattern. The final windows will have
        a width of 2 * area + 1. Default is 8.
    Returns
    -------
    detected_pattern : list
        A list of detected patterns in tuple form: (name, x, y, score).
    pileup_pattern : np.ndarray
        The 'pileup' (element-wise median) matrix of all patterns
        detected this way.
    """

    all_pattern_coords = []
    all_pattern_windows = []
    # TODO: remove chromosome loop from here, should be in main script
    for matrix, submat_idx, indices in zip(
        contact_map.sub_mats,
        contact_map.sub_mats_labels,
        contact_map.sub_mats_detectable_bins,
    ):
        # Pattern matching operate here
        mat_conv = corrcoef2d(matrix, kernel_matrix, kernel_config["max_dist"])
        mat_conv = mat_conv.tocoo()
        mat_conv.data[np.isnan(mat_conv.data)] = 0
        mat_conv.eliminate_zeros()
        # Find foci of highly correlated pixels
        chrom_pattern_coords = picker(mat_conv, kernel_config["precision"])
        # If no pattern was detected on this chromosome, go to next one
        if chrom_pattern_coords is None:
            continue
        filtered_chrom_patterns, chrom_pattern_windows = validate_patterns(
            chrom_pattern_coords,
            matrix,
            mat_conv.tocsr(),
            indices,
            kernel_matrix,
            kernel_config["max_perc_undetected"],
        )
        converted_coords = contact_map.get_full_mat_pattern(
            filtered_chrom_patterns, submat_idx
        )
        all_pattern_coords.append(converted_coords)
        all_pattern_windows.append(chrom_pattern_windows)

    # If no pattern detected on any chromosome, exit gracefully
    if len(all_pattern_coords) == 0:
        sys.stderr.write("No pattern detected ! Exiting.\n")
        sys.exit(0)

    # Combine patterns of all chromosomes into a single array
    all_pattern_coords = np.concatenate(all_pattern_coords, axis=0)
    all_pattern_windows = np.concatenate(all_pattern_windows, axis=2)

    # Make a pileup from all pattern windows
    pileup = pileup_patterns(all_pattern_windows)

    return all_pattern_coords, pileup


def explore_patterns(contact_map, kernel_config, window=4):
    """Explore patterns in a list of matrices

    Given a pattern type, attempt to detect that pattern in each matrix with
    confidence determined by the precision parameter. The detection is done
    in a multi-pass process:
    - First, pattern matching is done with the initial supplied kernels.
    - Then, an 'pileup' median pattern from all previously detected
    patterns is generated, and detection is done using this pattern for
    matching instead.
    - Repeat as needed or until convergence.

    Parameters
    ----------
    contact_map : ContactMap object
        Object containing the Hi-C contact map and all intra- + inter-
        chromosomal sub matrices as well as other attributes.
    kernel_config : dict
        Kernel configuration as documented in
        chromovision.utils.io.load_kernel_config
    window : int, optional
        The pattern window area. When a pattern is discovered in a previous
        pass, further detected patterns falling into that area are discarded.

    Returns
    -------
    all_patterns : set
        A set of patterns, each in the form (chrom, pos1, pos2, score).
    kernels : dictionary of lists of arrays
        A dictionary with one key per iteration where the values are lists of
        pileup patterns after each pass used as kernels in the next one.
        Takes the form: {1: [kernel1,...], 2: [kernel1,...], ...}
    list_current_pattern_count : list
        List of the number of patterns detected at each iteration.
    """

    # Dispatch detectors: the border detector has specificities while the
    # loop detector is more generic, so we use the generic one by default if
    # a pattern specific detector isn't implemented.

    # Init parameters for the while loop:
    #   - There's always at least one iteration (with the kernel)
    #   - Loop stops when the same number of patterns are detected after an
    #     iterations, or the max number of iterations has been specified and
    #     reached.
    #   - After the first pass, instead of the starting kernel the
    #     'pileup pattern' is used for pattern matching.

    all_patterns = set()
    hashed_neighborhoods = set()
    old_pattern_count, current_pattern_count = -1, 0
    list_current_pattern_count = []

    # Depending on matrix resolution, a pattern may be smeared over several
    # pixels. This trimming function ensures that there won't be many patterns
    # clustering around one location.
    def neigh_hash(coords, window):
        return (coords[0] // window, coords[1] // window)

    # Original kernels are loaded from a file, but next kernel will be "learnt"
    # from agglomerations at each iteration
    kernels = {i: [] for i in range(1, kernel_config["max_iterations"] + 1)}
    kernels["ori"] = kernel_config["kernels"]
    # Detect patterns at each iteration:
    # For iterations beyond the first, use pileup patterns
    # from previous iteration as kernel.
    for i in range(1, kernel_config["max_iterations"] + 1):
        # Stop trying if fewer patterns are detected than in previous iteration
        if old_pattern_count != current_pattern_count:
            old_pattern_count = current_pattern_count
            # Use 'original' kernels from files for the first iteration
            current_kernels = kernels["ori"] if i == 1 else kernels[i - 1]
            for kernel_matrix in current_kernels:
                # After first iteration, kernel_matrix is a pileup from the
                # previous iteration
                detected_coords, pileup_pattern = pattern_detector(
                    contact_map, kernel_config, kernel_matrix
                )
                for new_coords in detected_coords:
                    if (
                        neigh_hash(new_coords, window=window)
                        not in hashed_neighborhoods
                    ):
                        pos1, pos2, score = new_coords
                        pos1 = int(pos1)
                        pos2 = int(pos2)
                        all_patterns.add((pos1, pos2, score))
                        hashed_neighborhoods.add(neigh_hash(new_coords, window=window))
                # The pileup patterns of this iteration make up the kernel for the next one :)
                kernels[i].append(pileup_pattern)
            list_current_pattern_count.append(detected_coords.shape[0])

    # Remove original kernels, as we are only interested by pileup ones
    del kernels["ori"]
    return all_patterns, kernels, list_current_pattern_count


def picker(mat_conv, precision=None):
    """Pick pixels out of a convolution map
    Given a correlation heat map, pick (i, j) of local maxima
    Parameters
    ----------
    mat_conv : scipy.sparse.coo_matrix
        A float array assigning a correlation to each pixel (i,j)
        with the input kernel (e.g. loops).
    precision : float, optional
        Increasing this value reduces the amount of false positive patterns.
        This is the minimum number of standard deviations above the median of
        correlation coefficients required to consider a pixel as candidate.
    Returns
    -------
    foci_coords : numpy.array of ints
        2D array of coordinates for identified patterns.
    """

    candidate_mat = mat_conv.copy()
    candidate_mat = candidate_mat.tocoo()
    # Compute a threshold from precision arg and set all pixels below to 0
    thres = np.median(mat_conv.data) + precision * np.std(mat_conv.data)
    candidate_mat.data[candidate_mat.data < thres] = 0
    candidate_mat.data[candidate_mat.data != 0] = 1
    candidate_mat.eliminate_zeros()
    # Check if at least one candidate pixel was found
    if len(candidate_mat.data) > 0:
        num_foci, labelled_mat = label_connected_pixels_sparse(candidate_mat)
        # Will hold the coordinates of the best pixel for each focus
        foci_coords = np.zeros([num_foci, 2], int)
        # Iterate over candidate foci
        # NOTE: There can be jumps between foci id due to post labelling
        # removal of single pixel foci in label_connnected_pixels_sparse.
        # This is why we use focus_rank
        for focus_rank, focus_id in enumerate(np.unique(labelled_mat.data)):
            # Remember 1D indices of datapoint in focus
            focus_idx = np.where(labelled_mat.data == focus_id)[0]
            # Find index of max value within those indices
            focus_pixel_idx = np.argmax(mat_conv.data[focus_idx])
            # Retrieve row of original index
            original_pixel_idx = focus_idx[focus_pixel_idx]
            focus_pixel_row = labelled_mat.row[original_pixel_idx]
            focus_pixel_col = labelled_mat.col[original_pixel_idx]
            # Save coords of best pixels for this focus in a table
            foci_coords[focus_rank, 0] = focus_pixel_row
            foci_coords[focus_rank, 1] = focus_pixel_col
    else:
        foci_coords = None
    return foci_coords


def label_connected_pixels_sparse(matrix, min_focus_size=2):
    """
    Given a sparse matrix of 1 and 0 values, find
    all foci of continuously neighbouring positive pixels
    and assign them a label according to their focus. Diagonal,
    horizontal and vertical (8-way) adjacency is considered.

    Parameters
    ----------
    matrix : scipy.sparse.coo_matrix
        The input matrix where to label foci. Should be filled with 1
        and 0s.
    min_focus_size: int
        Minimum number of members required to keep a focus.
    
    Returns
    -------
    num_foci : int
        Number of individual foci identified.
    foci_mat : scipy.sparse.coo_matrix:
        The matrix with values replaced by their respective foci
        labels.
    
    Example
    -------
    >>> M.todense()
    array([[1 0 0 0]
           [1 0 1 0]
           [1 0 1 1]
           [0 0 0 0]])
    >>> num_foci, foci_mat = label_foci(M)
    >>> num_foci
    2
    >>>foci_mat.todense()
    array([[1 0 0 0]
           [1 0 2 0]
           [1 0 2 2]
           [0 0 0 0]])
    """

    candidates = matrix.copy()
    n_candidates = len(candidates.data)
    candidates.data = candidates.data.astype(bool)

    def get_1d_foci_transition(m):
        """
        Get a boolean array indicating if the next neighbour of
        each nonzero pixel will be in the same focus.
        """
        # Compute row and col index shifts between candidate pixels.
        # absolute value is used; left/right or up/down does not matter
        row_shift = np.abs(np.diff(m.row))
        col_shift = np.abs(np.diff(m.col))
        # Transform shifts to binary data: shifted by more than 1
        # from previous nonzero pixel ? Invert so that True means we
        # stay in the same focus.
        row_shift[row_shift < 2] = 0
        stay_foci_row = np.invert(row_shift.astype(bool))
        col_shift[col_shift < 2] = 0
        stay_foci_col = np.invert(col_shift.astype(bool))
        # Bitwise AND between row and col "stay arrays" to get
        # indices where neither the rows nor cols shift by more than 1
        # True at index i means: pixel i+1 in same focus as pixel i.
        stay_foci = np.bitwise_and(stay_foci_row, stay_foci_col)
        # Append False since there is no pixel after the last
        stay_foci = np.append(stay_foci, False)
        return stay_foci

    # Since we are using neighborhood with next nonzero pixel,
    # and nonzero pixels are sorted by rows, we need to do
    # the whole operation on the matrix and the transpose
    # (nonzero pixels sorted by columns). This will give both
    # the horizontal and vertical neighbourhood informations.
    stay_foci_hori = get_1d_foci_transition(candidates)
    stay_foci_verti = get_1d_foci_transition(candidates.T.tocsr().tocoo())
    # Candidates are sorted by rows in stay_foci_hori, but by col in
    # stay_foci_verti. We need to reorder stay_foci_verti to have them
    # in same order.
    ori_ids = candidates.copy()
    # store candidate ids in data
    ori_ids.data = np.array(range(len(ori_ids.row)), dtype=np.int)
    trans_ids = ori_ids.T  # Transposed matrix: candidates now sorted by column
    # Order is only updated if we convert types
    trans_ids = trans_ids.tocsr().tocoo()
    # Data can now be used as a transposed to original candidate id converter
    # e.g. if trans_idx.data[2] = 1, that  means the third (2) nonzero
    # value in transpose was the second (1) in original matrix
    # stay_foci_verti = stay_foci_verti[trans_ids.data]
    # Initialize adjacency matrix between candidate pixels
    adj_mat = lil_matrix((n_candidates, n_candidates))
    # Fill adjacency matrix using a stay_foci array

    def fill_adjacency_1d(adj, stay_foci, verti=False):
        """Fills adjacency matrix for all neighborhoods on 1 dimension"""
        for candidate_id, next_candidate_in_focus in enumerate(stay_foci):
            # If the next candidate will also be in focus, add fill connection
            # between current and next candidate in adjacency matrix
            if next_candidate_in_focus:
                if verti:
                    adj_from = trans_ids.data[candidate_id]
                    adj_to = trans_ids.data[candidate_id + 1]
                else:
                    adj_from = candidate_id
                    adj_to = candidate_id + 1
                adj[adj_from, adj_to] = 1
        return adj

    # Add horizontal-adjacency info
    adj_mat = fill_adjacency_1d(adj_mat, stay_foci_hori)
    # Add vertical-adjacency info.
    adj_mat = fill_adjacency_1d(adj_mat, stay_foci_verti, verti=True)
    # Now label foci by finding connected components
    num_foci, foci = connected_components(adj_mat)
    foci += 1  # We add 1 so that first spot is not 0
    foci = foci.astype(np.int64)
    # Remove foci with a single pixel
    for focus_num in range(1, num_foci + 1):
        if len(foci[foci == focus_num]) < min_focus_size:
            foci[foci == focus_num] = 0
    # mask small foci for removal
    good_foci = foci > 0
    # generate a new matrix, similar to the input, but where pixel values
    # are the foci ID of the pixel.
    foci_mat = coo_matrix(
        (foci[good_foci], (candidates.row[good_foci], candidates.col[good_foci])),
        shape=candidates.shape,
        dtype=np.int64,
    )
    return num_foci, foci_mat


def xcorr2(signal, kernel, max_scan_distance=None, threshold=1e-4):
    """Signal-kernel 2D convolution

    Convolution of a 2-dimensional signal (the contact map) with a kernel
    (the pattern template).

    Parameters
    ----------
    signal: scipy.sparse.csr_matrix
        A 2-dimensional numpy array Ms x Ns acting as the detrended Hi-C map.
    kernel: array_like
        A 2-dimensional numpy array Mk x Nk acting as the pattern template.
    max_scan_distance : int or None, optional
        Limits the range of computations beyond the diagonal. Default is None

    Returns
    -------
    out: scipy.sparse.coo_matrix
        Convolution product of signal by kernel.
    """

    sm, sn = signal.shape
    km, kn = kernel.shape

    # Kernel (half) height and width
    kh = (km - 1) // 2
    kw = (kn - 1) // 2

    if (km > sm) or (sn > sn):
        raise ValueError("cannot have kernel bigger than signal")

    if max_scan_distance is None:
        max_scan_distance = max(sm, sn)
    out = csc_matrix((sm - km + 1, sn - kn + 1), dtype=np.float64)

    signal = signal.tocsc()
    for ki in range(km):
        # Note convolution is only computed up to a distance from the diagonal
        for kj in range(kn):
            out += kernel[ki, kj] * signal[ki : sm - km + 1 + ki, kj : sn - kn + 1 + kj]

    # Set very low pixels to 0
    out.data[out.data < threshold] = 0
    out.eliminate_zeros()

    # Resize matrix: increment rows and cols by half kernel and set shape to input
    # matrix, effectively adding margins.
    out = out.tocoo()
    rows, cols = out.row + kh, out.col + kw
    out = coo_matrix((out.data, (rows, cols)), shape=(sm, sn), dtype=np.float64)

    return out


def corrcoef2d(signal, kernel, max_dist):
    """Signal-kernel 2D correlation

    Pearson correlation coefficient between signal and sliding kernel.
    """
    # Set diagonals that will overlap the kernel in the lower triangle to their
    # opposite diagonal (in upper triangl)
    signal = signal.tolil()
    for i in range(1, kernel.shape[0]):
        signal.setdiag(signal.diagonal(i), -i)

    # Kernel1 allows to compute the mean
    kernel1 = np.ones(kernel.shape) / kernel.size
    # Returns a matrix of means
    mean_signal = xcorr2(signal, kernel1, max_scan_distance=max_dist)
    std_signal = (
        np.abs(
            xcorr2(signal.power(2), kernel1, max_scan_distance=max_dist)
            - mean_signal.power(2)
        )
    ).sqrt()
    mean_kernel = np.mean(kernel)
    std_kernel = np.std(kernel)
    conv = xcorr2(signal, kernel / kernel.size, max_scan_distance=max_dist)

    # Since elementwise sparse matrices division is not implemented, compute
    # numerator and denominator and perform division on the 1D array of nonzero
    # values.
    numerator = conv - mean_signal * mean_kernel
    denominator = std_signal * std_kernel
    corrcoef = numerator.copy()
    # Get coords of non-zero (nz) values in the numerator
    nz_vals = corrcoef.nonzero()
    # Divide them by corresponding entries in the numerator
    denominator = denominator.tocsr()
    corrcoef.data /= denominator[nz_vals].A1
    corrcoef.data[corrcoef.data < 0] = 0
    # Only keep the upper triangle
    corrcoef = triu(corrcoef)
    import matplotlib.pyplot as plt

    plt.imshow(corrcoef.todense())
    plt.show()
    return corrcoef

