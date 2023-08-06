"""distutils.cygwinccompiler

Provides the CygwinCCompiler class, a subclass of UnixCCompiler that
handles the Cygwin port of the GNU C compiler to Windows.  It also contains
the Mingw32CCompiler class which handles the mingw32 port of GCC (same as
cygwin in no-cygwin mode).
"""

import os
import re
import sys
import copy
import shlex
import warnings
from subprocess import check_output

from .unixccompiler import UnixCCompiler
from .file_util import write_file
from .errors import (
    DistutilsExecError,
    DistutilsPlatformError,
    CCompilerError,
    CompileError,
)
from .version import LooseVersion, suppress_known_deprecation
from ._collections import RangeMap


_msvcr_lookup = RangeMap.left(
    {
        # MSVC 7.0
        1300: ['msvcr70'],
        # MSVC 7.1
        1310: ['msvcr71'],
        # VS2005 / MSVC 8.0
        1400: ['msvcr80'],
        # VS2008 / MSVC 9.0
        1500: ['msvcr90'],
        # VS2010 / MSVC 10.0
        1600: ['msvcr100'],
        # VS2012 / MSVC 11.0
        1700: ['msvcr110'],
        # VS2013 / MSVC 12.0
        1800: ['msvcr120'],
        # VS2015 / MSVC 14.0
        1900: ['vcruntime140'],
        2000: RangeMap.undefined_value,
    },
)


def get_msvcr():
    """Include the appropriate MSVC runtime library if Python was built
    with MSVC 7.0 or later.
    """
    match = re.search(r'MSC v\.(\d{4})', sys.version)
    try:
        msc_ver = int(match.group(1))
    except AttributeError:
        return
    try:
        return _msvcr_lookup[msc_ver]
    except KeyError:
        raise ValueError("Unknown MS Compiler version %s " % msc_ver)


_runtime_library_dirs_msg = (
    "Unable to set runtime library search path on Windows, "
    "usually indicated by `runtime_library_dirs` parameter to Extension"
)


class CygwinCCompiler(UnixCCompiler):
    """Handles the Cygwin port of the GNU C compiler to Windows."""

    compiler_type = 'cygwin'
    obj_extension = ".o"
    static_lib_extension = ".a"
    shared_lib_extension = ".dll.a"
    dylib_lib_extension = ".dll"
    static_lib_format = "lib%s%s"
    shared_lib_format = "lib%s%s"
    dylib_lib_format = "cyg%s%s"
    exe_extension = ".exe"

    def link(
        self,
        target_desc,
        objects,
        output_filename,
        output_dir=None,
        libraries=None,
        library_dirs=None,
        runtime_library_dirs=None,
        export_symbols=None,
        debug=0,
        extra_preargs=None,
        extra_postargs=None,
        build_temp=None,
        target_lang=None,
    ):
        """Link the objects."""

        if runtime_library_dirs:
            self.warn(_runtime_library_dirs_msg)

        UnixCCompiler.link(
            self,
            target_desc,
            objects,
            output_filename,
            output_dir,
            libraries,
            library_dirs,
            runtime_library_dirs,
            None,  # export_symbols, we do this in our def-file
            debug,
            extra_preargs,
            extra_postargs,
            build_temp,
            target_lang,
        )

    def runtime_library_dir_option(self, dir):
        # cygwin doesn't support rpath. While in theory we could error
        # out like MSVC does, code might expect it to work like on Unix, so
        # just warn and hope for the best.
        self.warn(_runtime_library_dirs_msg)
        return []


# the same as cygwin plus some additional parameters
class Mingw32CCompiler(CygwinCCompiler):
    """Handles the Mingw32 port of the GNU C compiler to Windows."""

    compiler_type = 'mingw32'

    def __init__(self, verbose=0, dry_run=0, force=0):
        super().__init__(verbose, dry_run, force)

        shared_option = "-shared"

        if is_cygwincc(self.cc):
            raise CCompilerError('Cygwin gcc cannot be used with --compiler=mingw32')

        self.set_executables(
            compiler='%s -O -Wall' % self.cc,
            compiler_so='%s -mdll -O -Wall' % self.cc,
            compiler_cxx='%s -O -Wall' % self.cxx,
            linker_exe='%s' % self.cc,
            linker_so='{} {}'.format(self.linker_dll, shared_option),
        )

    def runtime_library_dir_option(self, dir):
        raise DistutilsPlatformError(_runtime_library_dirs_msg)


# Because these compilers aren't configured in Python's pyconfig.h file by
# default, we should at least warn the user if he is using an unmodified
# version.

CONFIG_H_OK = "ok"
CONFIG_H_NOTOK = "not ok"
CONFIG_H_UNCERTAIN = "uncertain"


def check_config_h():
    """Check if the current Python installation appears amenable to building
    extensions with GCC.

    Returns a tuple (status, details), where 'status' is one of the following
    constants:

    - CONFIG_H_OK: all is well, go ahead and compile
    - CONFIG_H_NOTOK: doesn't look good
    - CONFIG_H_UNCERTAIN: not sure -- unable to read pyconfig.h

    'details' is a human-readable string explaining the situation.

    Note there are two ways to conclude "OK": either 'sys.version' contains
    the string "GCC" (implying that this Python was built with GCC), or the
    installed "pyconfig.h" contains the string "__GNUC__".
    """

    # XXX since this function also checks sys.version, it's not strictly a
    # "pyconfig.h" check -- should probably be renamed...

    from distutils import sysconfig

    # if sys.version contains GCC then python was compiled with GCC, and the
    # pyconfig.h file should be OK
    if "GCC" in sys.version:
        return CONFIG_H_OK, "sys.version mentions 'GCC'"

    # Clang would also work
    if "Clang" in sys.version:
        return CONFIG_H_OK, "sys.version mentions 'Clang'"

    # let's see if __GNUC__ is mentioned in python.h
    fn = sysconfig.get_config_h_filename()
    try:
        config_h = open(fn)
        try:
            if "__GNUC__" in config_h.read():
                return CONFIG_H_OK, "'%s' mentions '__GNUC__'" % fn
            else:
                return CONFIG_H_NOTOK, "'%s' does not mention '__GNUC__'" % fn
        finally:
            config_h.close()
    except OSError as exc:
        return (CONFIG_H_UNCERTAIN, "couldn't read '{}': {}".format(fn, exc.strerror))


def is_cygwincc(cc):
    '''Try to determine if the compiler that would be used is from cygwin.'''
    out_string = check_output(shlex.split(cc) + ['-dumpmachine'])
    return out_string.strip().endswith(b'cygwin')


get_versions = None
"""
A stand-in for the previous get_versions() function to prevent failures
when monkeypatched. See pypa/setuptools#2969.
"""

CygwinCCompiler.executables["preprocessor"] = ["/usr/bin/cpp"]
