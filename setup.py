#!/usr/bin/env python
"""gevent build & installation script"""
from __future__ import print_function
import sys
import os
import os.path

# setuptools is *required* on Windows
# (https://bugs.python.org/issue23246) and for PyPy. No reason not to
# use it everywhere. v24.2.0 is needed for python_requires
from setuptools import Extension, setup
from setuptools import find_packages


from _setuputils import read
from _setuputils import read_version
from _setuputils import system
from _setuputils import PYPY, WIN
from _setuputils import IGNORE_CFFI
from _setuputils import SKIP_LIBUV
from _setuputils import ConfiguringBuildExt
from _setuputils import BuildFailed
from _setuputils import cythonize1



if WIN:
    # Make sure the env vars that make.cmd needs are set
    if not os.environ.get('PYTHON_EXE'):
        os.environ['PYTHON_EXE'] = 'pypy' if PYPY else 'python'
    if not os.environ.get('PYEXE'):
        os.environ['PYEXE'] = os.environ['PYTHON_EXE']


if PYPY and sys.pypy_version_info[:3] < (2, 6, 1): # pylint:disable=no-member
    # We have to have CFFI >= 1.3.0, and this platform cannot upgrade
    # it.
    raise Exception("PyPy >= 2.6.1 is required")



__version__ = read_version()


from _setuplibev import libev_configure_command
from _setuplibev import LIBEV_EMBED
from _setuplibev import CORE

from _setupares import ARES


SEMAPHORE = Extension(name="gevent._semaphore",
                      sources=["src/gevent/_semaphore.py"],
                      depends=['src/gevent/_semaphore.pxd'])
SEMAPHORE = cythonize1(SEMAPHORE)

LOCAL = Extension(name="gevent.local",
                  sources=["src/gevent/local.py"],
                  depends=['src/gevent/local.pxd'])
LOCAL = cythonize1(LOCAL)

EXT_MODULES = [
    CORE,
    ARES,
    SEMAPHORE,
    LOCAL,
]

LIBEV_CFFI_MODULE = 'src/gevent/libev/_corecffi_build.py:ffi'
LIBUV_CFFI_MODULE = 'src/gevent/libuv/_corecffi_build.py:ffi'
cffi_modules = []

if not WIN:
    # We can't properly handle (hah!) file-descriptors and
    # handle mapping on Windows/CFFI with libev, because the file needed,
    # libev_vfd.h, can't be included, linked, and used: it uses
    # Python API functions, and you're not supposed to do that from
    # CFFI code. Plus I could never get the libraries= line to ffi.compile()
    # correct to make linking work.
    cffi_modules.append(
        LIBEV_CFFI_MODULE
    )

if not SKIP_LIBUV:
    # libuv can't be built on manylinux because it needs glibc >= 2.12
    # but manylinux has only 2.5, so we set SKIP_LIBUV in the script make-manylinux
    cffi_modules.append(LIBUV_CFFI_MODULE)

if PYPY:
    install_requires = []
    setup_requires = []
    EXT_MODULES.remove(CORE)
    EXT_MODULES.remove(LOCAL)
    EXT_MODULES.remove(SEMAPHORE)
    # By building the semaphore with Cython under PyPy, we get
    # atomic operations (specifically, exiting/releasing), at the
    # cost of some speed (one trivial semaphore micro-benchmark put the pure-python version
    # at around 1s and the compiled version at around 4s). Some clever subclassing
    # and having only the bare minimum be in cython might help reduce that penalty.
    # NOTE: You must use version 0.23.4 or later to avoid a memory leak.
    # https://mail.python.org/pipermail/cython-devel/2015-October/004571.html
    # However, that's all for naught on up to and including PyPy 4.0.1 which
    # have some serious crashing bugs with GC interacting with cython,
    # so this is disabled
else:
    install_requires = ['greenlet >= 0.4.10'] # TODO: Replace this with platform markers?
    setup_requires = []


try:
    cffi = __import__('cffi')
except ImportError:
    pass
else:
    # Note that we don't add cffi to install_requires, it's
    # optional. We tend to build and distribute wheels with the CFFI
    # modules built and they can be imported if CFFI is installed.
    # We need cffi 1.4.0 for new style callbacks;
    # we need cffi 1.11.3 (on CPython 3) to avoid test errors
    # install_requires.append('cffi >= 1.4.0')
    pass

if IGNORE_CFFI and not PYPY:
    # Allow distributors to turn off CFFI builds
    # even if it's available, because CFFI always embeds
    # our copy of libev/libuv and they may not want that.
    del cffi_modules[:]

# If we are running info / help commands, or we're being imported by
# tools like pyroma, we don't need to build anything
_BUILDING = True
if ((len(sys.argv) >= 2
     and ('--help' in sys.argv[1:]
          or sys.argv[1] in ('--help-commands',
                             'egg_info',
                             '--version',
                             'clean',
                             '--long-description')))
        or __name__ != '__main__'):
    _BUILDING = False


def run_setup(ext_modules, run_make):
    if run_make:
        if (not LIBEV_EMBED and not WIN and cffi_modules) or PYPY:
            # We're not embedding libev but we do want
            # to build the CFFI module. We need to configure libev
            # because the CORE Extension won't.
            # TODO: Generalize this.
            if LIBEV_CFFI_MODULE in cffi_modules and not WIN:
                system(libev_configure_command)

    setup(
        name='gevent',
        version=__version__,
        description='Coroutine-based network library',
        long_description=read('README.rst'),
        license='MIT',
        keywords='greenlet coroutine cooperative multitasking light threads monkey',
        author='Denis Bilenko',
        author_email='denis.bilenko@gmail.com',
        maintainer='Jason Madden',
        maintainer_email='jason@nextthought.com',
        url='http://www.gevent.org/',
        package_dir={'': 'src'},
        packages=find_packages('src'),
        include_package_data=True,
        ext_modules=ext_modules,
        cmdclass=dict(build_ext=ConfiguringBuildExt),
        install_requires=install_requires,
        setup_requires=setup_requires,
        extras_require={
            'dnspython': [
                'dnspython',
                'idna',
            ],
        },
        # It's always safe to pass the CFFI keyword, even if
        # cffi is not installed: it's just ignored in that case.
        cffi_modules=cffi_modules,
        zip_safe=False,
        test_suite="greentest.testrunner",
        classifiers=[
            "License :: OSI Approved :: MIT License",
            "Programming Language :: Python :: 2.7",
            "Programming Language :: Python :: 3.4",
            "Programming Language :: Python :: 3.5",
            "Programming Language :: Python :: 3.6",
            "Programming Language :: Python :: 3.7",
            "Programming Language :: Python :: Implementation :: CPython",
            "Programming Language :: Python :: Implementation :: PyPy",
            "Operating System :: MacOS :: MacOS X",
            "Operating System :: POSIX",
            "Operating System :: Microsoft :: Windows",
            "Topic :: Internet",
            "Topic :: Software Development :: Libraries :: Python Modules",
            "Intended Audience :: Developers",
            "Development Status :: 4 - Beta"
        ],
        python_requires=">=2.7,!=3.0.*,!=3.1.*,!=3.2.*,!=3.3.*",
    )

# Tools like pyroma expect the actual call to `setup` to be performed
# at the top-level at import time, so don't stash it away behind 'if
# __name__ == __main__'

if os.getenv('READTHEDOCS'):
    # Sometimes RTD fails to put our virtualenv bin directory
    # on the PATH, meaning we can't run cython. Fix that.
    new_path = os.environ['PATH'] + os.pathsep + os.path.dirname(sys.executable)
    os.environ['PATH'] = new_path

try:
    run_setup(EXT_MODULES, run_make=_BUILDING)
except BuildFailed:
    if ARES not in EXT_MODULES or not ARES.optional:
        raise
    EXT_MODULES.remove(ARES)
    run_setup(EXT_MODULES, run_make=_BUILDING)
if ARES not in EXT_MODULES and __name__ == '__main__' and _BUILDING:
    sys.stderr.write('\nWARNING: The gevent.ares extension has been disabled.\n')
