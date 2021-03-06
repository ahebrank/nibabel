""" Validate image proxy API

Minimum array proxy API is:

* read only shape attribute / property
* read only is_proxy attribute / property
* returns array from np.asarray(prox)

And:

* that modifying no object outside ``prox`` will affect the result of
  ``np.asarray(obj)``.  Specifically:
  * Changes in position (``obj.tell()``) of any passed file-like objects
    will not affect the output of from ``np.asarray(proxy)``.
  * if you pass a header into the __init__, then modifying the original
    header will not affect the result of the array return.

These last are to allow the proxy to be re-used with different images.
"""
from __future__ import division, print_function, absolute_import

from os.path import join as pjoin
import warnings
from io import BytesIO

import numpy as np

from ..externals.six import string_types
from ..volumeutils import apply_read_scaling
from ..analyze import AnalyzeHeader
from ..spm99analyze import Spm99AnalyzeHeader
from ..spm2analyze import Spm2AnalyzeHeader
from ..nifti1 import Nifti1Header
from ..freesurfer.mghformat import MGHHeader
from .. import minc1
from ..externals.netcdf import netcdf_file
from .. import minc2
from ..optpkg import optional_package
h5py, have_h5py, _ = optional_package('h5py')
from .. import ecat

from ..arrayproxy import ArrayProxy

from nose import SkipTest
from nose.tools import (assert_true, assert_false, assert_raises,
                        assert_equal, assert_not_equal)

from numpy.testing import (assert_almost_equal, assert_array_equal)

from ..testing import data_path as DATA_PATH

from ..tmpdirs import InTemporaryDirectory

from .test_api_validators import ValidateAPI


class _TestProxyAPI(ValidateAPI):
    """ Base class for testing proxy APIs

    Assumes that real classes will provide an `obj_params` method which is a
    generator returning 2 tuples of (<proxy_maker>, <param_dict>).
    <proxy_maker> is a function returning a 3 tuple of (<proxy>, <fileobj>,
    <header>).  <param_dict> is a dictionary containing at least keys
    ``arr_out`` (expected output array from proxy), ``dtype_out`` (expected
    output dtype for array) and ``shape`` (shape of array).

    The <header> above should support at least "get_data_dtype",
    "set_data_dtype", "get_data_shape", "set_data_shape"
    """
    # Flag True if offset can be set into header of image
    settable_offset = False

    def validate_shape(self, pmaker, params):
        # Check shape
        prox, fio, hdr = pmaker()
        assert_array_equal(prox.shape, params['shape'])
        # Read only
        assert_raises(AttributeError, setattr, prox, 'shape', params['shape'])

    def validate_is_proxy(self, pmaker, params):
        # Check shape
        prox, fio, hdr = pmaker()
        assert_true(prox.is_proxy)
        # Read only
        assert_raises(AttributeError, setattr, prox, 'is_proxy', False)

    def validate_asarray(self, pmaker, params):
        # Check proxy returns expected array from asarray
        prox, fio, hdr = pmaker()
        out = np.asarray(prox)
        assert_array_equal(out, params['arr_out'])
        assert_equal(out.dtype.type, params['dtype_out'])
        # Shape matches expected shape
        assert_equal(out.shape, params['shape'])

    def validate_header_isolated(self, pmaker, params):
        # Confirm altering input header has no effect
        # Depends on header providing 'get_data_dtype', 'set_data_dtype',
        # 'get_data_shape', 'set_data_shape', 'set_data_offset'
        prox, fio, hdr = pmaker()
        assert_array_equal(prox, params['arr_out'])
        # Mess up header badly and hope for same correct result
        if hdr.get_data_dtype() == np.uint8:
            hdr.set_data_dtype(np.int16)
        else:
            hdr.set_data_dtype(np.uint8)
        hdr.set_data_shape(np.array(hdr.get_data_shape()) + 1)
        if self.settable_offset:
            hdr.set_data_offset(32)
        assert_array_equal(prox, params['arr_out'])

    def validate_fileobj_isolated(self, pmaker, params):
        # Check file position of read independent of file-like object
        prox, fio, hdr = pmaker()
        if isinstance(fio, string_types):
            return
        assert_array_equal(prox, params['arr_out'])
        fio.read() # move to end of file
        assert_array_equal(prox, params['arr_out'])


class TestAnalyzeProxyAPI(_TestProxyAPI):
    """ Specific Analyze-type array proxy API test

    The analyze proxy extends the general API by adding read-only attributes
    ``slope, inter, offset``
    """
    proxy_class = ArrayProxy
    header_class = AnalyzeHeader
    shapes = ((2,), (2, 3), (2, 3, 4), (2, 3, 4, 5))
    has_slope = False
    has_inter = False
    array_order = 'F'
    # Cannot set offset for Freesurfer
    settable_offset = True
    # Freesurfer enforces big-endian. '=' means use native
    data_endian = '='

    def obj_params(self):
        """ Iterator returning (``proxy_creator``, ``proxy_params``) pairs

        Each pair will be tested separately.

        ``proxy_creator`` is a function taking no arguments and returning (fresh
        proxy object, fileobj, header).  We need to pass this function rather
        than a proxy instance so we can recreate the proxy objects fresh for
        each of multiple tests run from the ``validate_xxx`` autogenerated test
        methods.  This allows the tests to modify the proxy instance without
        having an effect on the later tests in the same function.
        """
        # Analyze and up wrap binary arrays, Fortran ordered, with given offset
        # and dtype and shape.
        if not self.settable_offset:
            offsets = (self.header_class().get_data_offset(),)
        else:
            offsets = (0, 16)
        for shape in self.shapes:
            n_els = np.prod(shape)
            for dtype in (np.uint8, np.int16, np.float32):
                dt = np.dtype(dtype).newbyteorder(self.data_endian)
                arr = np.arange(n_els, dtype=dt).reshape(shape)
                data = arr.tostring(order=self.array_order)
                for offset in offsets:
                    slopes = (1., 2.) if self.has_slope else (1.,)
                    inters = (0., 10.) if self.has_inter else (0.,)
                    for slope in slopes:
                        for inter in inters:
                            hdr = self.header_class()
                            hdr.set_data_dtype(dtype)
                            hdr.set_data_shape(shape)
                            if self.settable_offset:
                                hdr.set_data_offset(offset)
                            if (slope, inter) == (1, 0): # No scaling applied
                                # dtype from array
                                dtype_out = dtype
                            else: # scaling or offset applied
                                # out dtype predictable from apply_read_scaling
                                # and datatypes of slope, inter
                                hdr.set_slope_inter(slope, inter)
                                s, i = hdr.get_slope_inter()
                                tmp = apply_read_scaling(arr,
                                                         1. if s is None else s,
                                                         0. if i is None else i)
                                dtype_out = tmp.dtype.type
                            def sio_func():
                                fio = BytesIO()
                                fio.truncate(0)
                                fio.seek(offset)
                                fio.write(data)
                                # Use a copy of the header to avoid changing
                                # global header in test functions.
                                new_hdr = hdr.copy()
                                return (self.proxy_class(fio, new_hdr),
                                        fio,
                                        new_hdr)
                            params = dict(
                                dtype = dtype,
                                dtype_out = dtype_out,
                                arr = arr.copy(),
                                arr_out = arr * slope + inter,
                                shape = shape,
                                offset = offset,
                                slope = slope,
                                inter = inter)
                            yield sio_func, params
                            # Same with filenames
                            with InTemporaryDirectory():
                                fname = 'data.bin'
                                def fname_func():
                                    with open(fname, 'wb') as fio:
                                        fio.seek(offset)
                                        fio.write(data)
                                    # Use a copy of the header to avoid changing
                                    # global header in test functions.
                                    new_hdr = hdr.copy()
                                    return (self.proxy_class(fname, new_hdr),
                                            fname,
                                            new_hdr)
                                params = params.copy()
                                yield fname_func, params

    def validate_slope_inter_offset(self, pmaker, params):
        # Check slope, inter, offset
        prox, fio, hdr = pmaker()
        for attr_name in ('slope', 'inter', 'offset'):
            expected = params[attr_name]
            assert_array_equal(getattr(prox, attr_name), expected)
            # Read only
            assert_raises(AttributeError,
                          setattr, prox, attr_name, expected)

    def validate_deprecated_header(self, pmaker, params):
        prox, fio, hdr = pmaker()
        with warnings.catch_warnings(record=True) as warns:
            warnings.simplefilter("always")
            # Header is a copy of original
            assert_false(prox.header is hdr)
            assert_equal(prox.header, hdr)
            assert_equal(warns.pop(0).category, FutureWarning)


class TestSpm99AnalyzeProxyAPI(TestAnalyzeProxyAPI):
    # SPM-type analyze has slope scaling but not intercept
    header_class = Spm99AnalyzeHeader
    has_slope = True


class TestSpm2AnalyzeProxyAPI(TestSpm99AnalyzeProxyAPI):
    header_class = Spm2AnalyzeHeader


class TestNifti1ProxyAPI(TestSpm99AnalyzeProxyAPI):
    header_class = Nifti1Header
    has_inter = True


class TestMGHAPI(TestAnalyzeProxyAPI):
    header_class = MGHHeader
    shapes = ((2, 3, 4), (2, 3, 4, 5)) # MGH can only do >= 3D
    has_slope = False
    has_inter = False
    settable_offset = False
    data_endian = '>'


class TestMinc1API(_TestProxyAPI):
    module = minc1
    file_class = minc1.Minc1File
    eg_fname = 'tiny.mnc'
    eg_shape = (10, 20, 20)
    opener = netcdf_file

    def obj_params(self):
        """ Iterator returning (``proxy_creator``, ``proxy_params``) pairs

        Each pair will be tested separately.

        ``proxy_creator`` is a function taking no arguments and returning (fresh
        proxy object, fileobj, header).  We need to pass this function rather
        than a proxy instance so we can recreate the proxy objects fresh for
        each of multiple tests run from the ``validate_xxx`` autogenerated test
        methods.  This allows the tests to modify the proxy instance without
        having an effect on the later tests in the same function.
        """
        eg_path = pjoin(DATA_PATH, self.eg_fname)
        arr_out = self.file_class(
            self.opener(eg_path)).get_scaled_data()
        def eg_func():
            mf = self.file_class(self.opener(eg_path))
            prox = minc1.MincImageArrayProxy(mf)
            img = self.module.load(eg_path)
            fobj = open(eg_path, 'rb')
            return prox, fobj, img.header
        yield (eg_func,
               dict(shape=self.eg_shape,
                    dtype_out=np.float64,
                    arr_out=arr_out))


if have_h5py:
    class TestMinc2API(TestMinc1API):
        module = minc2
        file_class = minc2.Minc2File
        eg_fname = 'small.mnc'
        eg_shape = (18, 28, 29)
        opener = h5py.File


class TestEcatAPI(_TestProxyAPI):
    eg_fname = 'tinypet.v'
    eg_shape = (10, 10, 3, 1)

    def obj_params(self):
        eg_path = pjoin(DATA_PATH, self.eg_fname)
        img = ecat.load(eg_path)
        arr_out = img.get_data()
        def eg_func():
            img = ecat.load(eg_path)
            sh = img.get_subheaders()
            prox = ecat.EcatImageArrayProxy(sh)
            fobj = open(eg_path, 'rb')
            return prox, fobj, sh
        yield (eg_func,
               dict(shape=self.eg_shape,
                    dtype_out=np.float64,
                    arr_out=arr_out))

    def validate_header_isolated(self, pmaker, params):
        raise SkipTest('ECAT header does not support dtype get')
