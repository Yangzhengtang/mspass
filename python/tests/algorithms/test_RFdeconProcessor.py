#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import pickle
import numpy as np
import pytest

sys.path.append("python/tests")
from helper import (get_live_seismogram,
                    get_live_timeseries,
                    get_sin_timeseries,
                    get_live_timeseries_ensemble,
                    get_live_seismogram_ensemble)

from mspasspy.algorithms.RFdeconProcessor import RFdeconProcessor

def test_RFdeconProcessor():
    decon_processor = RFdeconProcessor(alg="MultiTaperXcor")

    seis_data = get_live_seismogram()
    seis_wavelet = get_live_seismogram()
    seis_noise = get_live_seismogram()

    decon_processor.loaddata(seis_data)
    decon_processor.loadwavelet(seis_wavelet)
    decon_processor.loadnoise(seis_noise)

    # decon_processor_copy = pickle.loads(pickle.dumps(decon_processor))
    data = pickle.dumps(decon_processor)
    decon_processor_copy = pickle.loads(data)

    assert (decon_processor.dvector == decon_processor_copy.dvector).all()
    assert (decon_processor.wvector == decon_processor_copy.wvector).all()
    assert (decon_processor.nvector == decon_processor_copy.nvector).all()

    seis_data.npts = 8
    seis_data.data[2] = [1,-1,0,0,0,0,0,0]
    seis_data.data[1] = [0,1,-1,0,0,0,0,0]
    seis_data.data[0] = [0,0,-1,1,0,0,0,0]

    decon_processor = RFdeconProcessor()
    decon_processor.loaddata(seis_data)
    decon_processor.loadwavelet(seis_data)
    decon_processor_copy = pickle.loads(pickle.dumps(decon_processor))
    result1 = np.array(decon_processor.apply())
    result2 = np.array(decon_processor_copy.apply())
    assert all(abs(a-b) < 1e-6 for a,b in zip(result1, result2))
