import circus.shared.algorithms as algo
from .shared.utils import *
from .shared.files import get_dead_times
from .shared.probes import get_nodes_and_edges
from circus.shared.messages import print_and_log, init_logging
from circus.shared.mpi import detect_memory

def main(params, nb_cpu, nb_gpu, use_gpu):

    #################################################################
    #params         = detect_memory(params)
    logger         = init_logging(params.logfile)
    SHARED_MEMORY  = get_shared_memory_flag(params)
    logger         = logging.getLogger('circus.fitting')
    data_file      = params.data_file
    data_file.open()
    N_e            = params.getint('data', 'N_e')
    N_total        = params.nb_channels
    N_t            = params.getint('detection', 'N_t')
    template_shift = params.getint('detection', 'template_shift')
    file_out       = params.get('data', 'file_out')
    file_out_suff  = params.get('data', 'file_out_suff')
    sign_peaks     = params.get('detection', 'peaks')
    dist_peaks     = params.getint('detection', 'dist_peaks')
    matched_filter = params.getboolean('detection', 'matched-filter')
    spike_thresh   = params.getfloat('detection', 'spike_thresh')
    do_temporal_whitening = params.getboolean('whitening', 'temporal')
    do_spatial_whitening  = params.getboolean('whitening', 'spatial')
    chunk_size     = detect_memory(params)
    gpu_only       = params.getboolean('fitting', 'gpu_only')
    nodes, edges   = get_nodes_and_edges(params)
    tmp_limits     = params.get('fitting', 'amp_limits').replace('(', '').replace(')', '').split(',')
    tmp_limits     = map(float, tmp_limits)
    amp_auto       = params.getboolean('fitting', 'amp_auto')
    nb_chances     = params.getint('fitting', 'nb_chances')
    max_chunk      = params.getfloat('fitting', 'max_chunk')
    noise_thr      = params.getfloat('clustering', 'noise_thr')
    collect_all    = params.getboolean('fitting', 'collect_all')
    ignore_dead_times = params.getboolean('triggers', 'ignore_times')
    inv_nodes         = numpy.zeros(N_total, dtype=numpy.int32)
    inv_nodes[nodes]  = numpy.argsort(nodes)
    #################################################################

    if use_gpu:
        import cudamat as cmt
        ## Need to properly handle multi GPU per MPI nodes?
        if nb_gpu > nb_cpu:
            gpu_id = int(comm.rank//nb_cpu)
        else:
            gpu_id = 0
        cmt.cuda_set_device(gpu_id)
        cmt.init()
        cmt.cuda_sync_threads()

    if matched_filter:
        if sign_peaks in ['negative', 'both']:
            waveform_neg  = io.load_data(params, 'waveform')
            waveform_neg /= (numpy.abs(numpy.sum(waveform_neg))* len(waveform_neg))
            matched_tresholds_neg = io.load_data(params, 'matched-thresholds')
        if sign_peaks in ['positive', 'both']:
            waveform_pos  = io.load_data(params, 'waveform-pos')
            waveform_pos /= (numpy.abs(numpy.sum(waveform_pos))* len(waveform_pos))
            matched_tresholds_pos = io.load_data(params, 'matched-thresholds-pos')

    if ignore_dead_times:
        all_dead_times = get_dead_times(params)

    thresholds = io.load_data(params, 'thresholds')

    comm.Barrier()

    if comm.rank == 0:
        print_and_log(["Extracting MUA activity..."], 'default', logger)
        purge(file_out_suff, '.data')

    if do_spatial_whitening:
        spatial_whitening  = io.load_data(params, 'spatial_whitening')
    if do_temporal_whitening:
        temporal_whitening = io.load_data(params, 'temporal_whitening')


    nb_chunks, last_chunk_len = data_file.analyze(chunk_size)
    processed_chunks          = int(min(nb_chunks, max_chunk))

    spiketimes_file = open(file_out_suff + '.mua-%d.data' %comm.rank, 'wb')
    comm.Barrier()
    electrodes_file = open(file_out_suff + '.elec-%d.data' %comm.rank, 'wb')
    comm.Barrier()
    amplitudes_file = open(file_out_suff + '.amp-%d.data' %comm.rank, 'wb')
    comm.Barrier()

    if use_gpu and do_spatial_whitening:
        spatial_whitening = cmt.CUDAMatrix(spatial_whitening, copy_on_host=False)

    to_explore = xrange(comm.rank, processed_chunks, comm.size)

    if comm.rank == 0:
        to_explore = get_tqdm_progressbar(to_explore)

    for gcount, gidx in enumerate(to_explore):
        #print "Node", comm.rank, "is analyzing chunk", gidx, "/", nb_chunks, " ..."
        ## We need to deal with the borders by taking chunks of size [0, chunck_size+template_shift]

        is_first = data_file.is_first_chunk(gidx, nb_chunks)
        is_last  = data_file.is_last_chunk(gidx, nb_chunks)

        if is_last:
            padding = (-dist_peaks, 0)
        elif is_first:
            padding = (0, dist_peaks)
        else:
            padding = (-dist_peaks, dist_peaks)

        result       = {'spiketimes' : [], 'amplitudes' : [], 'templates' : []}

        local_chunk, t_offset = data_file.get_data(gidx, chunk_size, padding, nodes=nodes)           
        len_chunk             = len(local_chunk)

        if do_spatial_whitening:
            if use_gpu:
                local_chunk = cmt.CUDAMatrix(local_chunk, copy_on_host=False)
                local_chunk = local_chunk.dot(spatial_whitening).asarray()
            else:
                local_chunk = numpy.dot(local_chunk, spatial_whitening)
        if do_temporal_whitening:
            local_chunk = scipy.ndimage.filters.convolve1d(local_chunk, temporal_whitening, axis=0, mode='constant')

        #print "Extracting the peaks..."

        local_peaktimes = numpy.zeros(0, dtype=numpy.uint32)
        local_elecs     = numpy.zeros(0, dtype=numpy.uint32)
        local_amps      = numpy.zeros(0, dtype=numpy.float32)

        if matched_filter:
            if sign_peaks in ['positive', 'both']:
                filter_chunk = scipy.ndimage.filters.convolve1d(local_chunk, waveform_pos, axis=0, mode='constant')
                for i in xrange(N_e):
                    peaktimes = algo.detect_peaks(filter_chunk[:, i], matched_tresholds_pos[i], mpd=dist_peaks)
                    local_peaktimes = numpy.concatenate((local_peaktimes, peaktimes))
                    local_elecs = numpy.concatenate((local_elecs, i*numpy.ones(len(peaktimes), dtype='uint32')))
                    local_amps = numpy.concatenate((local_amps, filter_chunk[peaktimes, i]))
            if sign_peaks in ['negative', 'both']:
                filter_chunk = scipy.ndimage.filters.convolve1d(local_chunk, waveform_neg, axis=0, mode='constant')
                for i in xrange(N_e):
                    peaktimes = algo.detect_peaks(filter_chunk[:, i], matched_tresholds_neg[i], mpd=dist_peaks)
                    local_peaktimes = numpy.concatenate((local_peaktimes, peaktimes))
                    local_elecs = numpy.concatenate((local_elecs, i*numpy.ones(len(peaktimes), dtype='uint32')))
                    local_amps = numpy.concatenate((local_amps, filter_chunk[peaktimes, i]))
        else:
            for i in xrange(N_e):
                if sign_peaks == 'negative':
                    peaktimes = algo.detect_peaks(local_chunk[:, i], thresholds[i], valley=True, mpd=dist_peaks)
                elif sign_peaks == 'positive':
                    peaktimes = algo.detect_peaks(local_chunk[:, i], thresholds[i], valley=False, mpd=dist_peaks)
                elif sign_peaks == 'both':
                    peaktimes = algo.detect_peaks(numpy.abs(local_chunk[:, i]), thresholds[i], valley=False, mpd=dist_peaks)          
                local_peaktimes = numpy.concatenate((local_peaktimes, peaktimes)) 
                local_elecs = numpy.concatenate((local_elecs, i*numpy.ones(len(peaktimes), dtype='uint32')))
                local_amps = numpy.concatenate((local_amps, local_chunk[peaktimes, i]))

        g_offset = t_offset + padding[0]

        if ignore_dead_times:
            dead_indices = numpy.searchsorted(all_dead_times, [t_offset, t_offset + chunk_size])
            if dead_indices[0] != dead_indices[1]:
                local_peaktimes = numpy.array(list(set(local_peaktimes + g_offset).difference(all_dead_times[dead_indices[0]:dead_indices[1]])), dtype=numpy.uint32) - g_offset
                local_peaktimes = numpy.sort(local_peaktimes)

        #print "Removing the useless borders..."
        local_borders   = (0, len_chunk - dist_peaks)
        idx             = (local_peaktimes >= local_borders[0]) & (local_peaktimes < local_borders[1])
        local_peaktimes = numpy.compress(idx, local_peaktimes) + g_offset
        local_elecs     = numpy.compress(idx, local_elecs)
        local_amps      = numpy.compress(idx, local_amps)

        spiketimes_file.write(local_peaktimes.tostring())
        electrodes_file.write(local_elecs.tostring())
        amplitudes_file.write(local_amps.tostring())


    sys.stderr.flush()

    spiketimes_file.flush()
    os.fsync(spiketimes_file.fileno())
    spiketimes_file.close()

    electrodes_file.flush()
    os.fsync(electrodes_file.fileno())
    electrodes_file.close()

    amplitudes_file.flush()
    os.fsync(amplitudes_file.fileno())
    amplitudes_file.close()

    comm.Barrier()
    
    if comm.rank == 0:
        io.collect_mua(comm.size, params, erase=True)

    data_file.close()