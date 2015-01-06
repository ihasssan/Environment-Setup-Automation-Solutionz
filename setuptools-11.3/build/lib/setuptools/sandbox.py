import os
import sys
import tempfile
import operator
import functools
import itertools
import re
import contextlib
import pickle

import pkg_resources

if os.name == "java":
    import org.python.modules.posix.PosixModule as _os
else:
    _os = sys.modules[os.name]
try:
    _file = file
except NameError:
    _file = None
_open = open
from distutils.errors import DistutilsError
from pkg_resources import working_set

from setuptools import compat
from setuptools.compat import builtins

__all__ = [
    "AbstractSandbox", "DirectorySandbox", "SandboxViolation", "run_setup",
]

def _execfile(filename, globals, locals=None):
    """
    Python 3 implementation of execfile.
    """
    mode = 'rb'
    # Python 2.6 compile requires LF for newlines, so use deprecated
    #  Universal newlines support.
    if sys.version_info < (2, 7):
        mode += 'U'
    with open(filename, mode) as stream:
        script = stream.read()
    if locals is None:
        locals = globals
    code = compile(script, filename, 'exec')
    exec(code, globals, locals)


@contextlib.contextmanager
def save_argv():
    saved = sys.argv[:]
    try:
        yield saved
    finally:
        sys.argv[:] = saved


@contextlib.contextmanager
def save_path():
    saved = sys.path[:]
    try:
        yield saved
    finally:
        sys.path[:] = saved


@contextlib.contextmanager
def override_temp(replacement):
    """
    Monkey-patch tempfile.tempdir with replacement, ensuring it exists
    """
    if not os.path.isdir(replacement):
        os.makedirs(replacement)

    saved = tempfile.tempdir

    tempfile.tempdir = replacement

    try:
        yield
    finally:
        tempfile.tempdir = saved


@contextlib.contextmanager
def pushd(target):
    saved = os.getcwd()
    os.chdir(target)
    try:
        yield saved
    finally:
        os.chdir(saved)


@contextlib.contextmanager
def save_modules():
    """
    Context in which imported modules are saved.

    Translates exceptions internal to the context into the equivalent exception
    outside the context.
    """
    saved = sys.modules.copy()
    try:
        try:
            yield saved
        except:
            # dump any exception
            class_, exc, tb = sys.exc_info()
            saved_cls = pickle.dumps(class_)
            saved_exc = pickle.dumps(exc)
            raise
        finally:
            sys.modules.update(saved)
            # remove any modules imported since
            del_modules = (
                mod_name for mod_name in sys.modules
                if mod_name not in saved
                # exclude any encodings modules. See #285
                and not mod_name.startswith('encodings.')
            )
            _clear_modules(del_modules)
    except:
        # reload and re-raise any exception, using restored modules
        class_, exc, tb = sys.exc_info()
        new_cls = pickle.loads(saved_cls)
        new_exc = pickle.loads(saved_exc)
        compat.reraise(new_cls, new_exc, tb)


def _clear_modules(module_names):
    for mod_name in list(module_names):
        del sys.modules[mod_name]


@contextlib.contextmanager
def save_pkg_resources_state():
    saved = pkg_resources.__getstate__()
    try:
        yield saved
    finally:
        pkg_resources.__setstate__(saved)


@contextlib.contextmanager
def setup_context(setup_dir):
    temp_dir = os.path.join(setup_dir, 'temp')
    with save_pkg_resources_state():
        with save_modules():
            hide_setuptools()
            with save_path():
                with save_argv():
                    with override_temp(temp_dir):
                        with pushd(setup_dir):
                            # ensure setuptools commands are available
                            __import__('setuptools')
                            yield


def _needs_hiding(mod_name):
    """
    >>> _needs_hiding('setuptools')
    True
    >>> _needs_hiding('pkg_resources')
    True
    >>> _needs_hiding('setuptools_plugin')
    False
    >>> _needs_hiding('setuptools.__init__')
    True
    >>> _needs_hiding('distutils')
    True
    """
    pattern = re.compile('(setuptools|pkg_resources|distutils)(\.|$)')
    return bool(pattern.match(mod_name))


def hide_setuptools():
    """
    Remove references to setuptools' modules from sys.modules to allow the
    invocation to import the most appropriate setuptools. This technique is
    necessary to avoid issues such as #315 where setuptools upgrading itself
    would fail to find a function declared in the metadata.
    """
    modules = filter(_needs_hiding, sys.modules)
    _clear_modules(modules)


def run_setup(setup_script, args):
    """Run a distutils setup script, sandboxed in its directory"""
    setup_dir = os.path.abspath(os.path.dirname(setup_script))
    with setup_context(setup_dir):
        try:
            sys.argv[:] = [setup_script]+list(args)
            sys.path.insert(0, setup_dir)
            # reset to include setup dir, w/clean callback list
            working_set.__init__()
            working_set.callbacks.append(lambda dist:dist.activate())
            def runner():
                ns = dict(__file__=setup_script, __name__='__main__')
                _execfile(setup_script, ns)
            DirectorySandbox(setup_dir).run(runner)
        except SystemExit as v:
            if v.args and v.args[0]:
                raise
            # Normal exit, just return


class AbstractSandbox:
    """Wrap 'os' module and 'open()' builtin for virtualizing setup scripts"""

    _active = False

    def __init__(self):
        self._attrs = [
            name for name in dir(_os)
                if not name.startswith('_') and hasattr(self,name)
        ]

    def _copy(self, source):
        for name in self._attrs:
            setattr(os, name, getattr(source,name))

    def run(self, func):
        """Run 'func' under os sandboxing"""
        try:
            self._copy(self)
            if _file:
                builtins.file = self._file
            builtins.open = self._open
            self._active = True
            return func()
        finally:
            self._active = False
            if _file:
                builtins.file = _file
            builtins.open = _open
            self._copy(_os)

    def _mk_dual_path_wrapper(name):
        original = getattr(_os,name)
        def wrap(self,src,dst,*args,**kw):
            if self._active:
                src,dst = self._remap_pair(name,src,dst,*args,**kw)
            return original(src,dst,*args,**kw)
        return wrap

    for name in ["rename", "link", "symlink"]:
        if hasattr(_os,name): locals()[name] = _mk_dual_path_wrapper(name)

    def _mk_single_path_wrapper(name, original=None):
        original = original or getattr(_os,name)
        def wrap(self,path,*args,**kw):
            if self._active:
                path = self._remap_input(name,path,*args,**kw)
            return original(path,*args,**kw)
        return wrap

    if _file:
        _file = _mk_single_path_wrapper('file', _file)
    _open = _mk_single_path_wrapper('open', _open)
    for name in [
        "stat", "listdir", "chdir", "open", "chmod", "chown", "mkdir",
        "remove", "unlink", "rmdir", "utime", "lchown", "chroot", "lstat",
        "startfile", "mkfifo", "mknod", "pathconf", "access"
    ]:
        if hasattr(_os,name): locals()[name] = _mk_single_path_wrapper(name)

    def _mk_single_with_return(name):
        original = getattr(_os,name)
        def wrap(self,path,*args,**kw):
            if self._active:
                path = self._remap_input(name,path,*args,**kw)
                return self._remap_output(name, original(path,*args,**kw))
            return original(path,*args,**kw)
        return wrap

    for name in ['readlink', 'tempnam']:
        if hasattr(_os,name): locals()[name] = _mk_single_with_return(name)

    def _mk_query(name):
        original = getattr(_os,name)
        def wrap(self,*args,**kw):
            retval = original(*args,**kw)
            if self._active:
                return self._remap_output(name, retval)
            return retval
        return wrap

    for name in ['getcwd', 'tmpnam']:
        if hasattr(_os,name): locals()[name] = _mk_query(name)

    def _validate_path(self,path):
        """Called to remap or validate any path, whether input or output"""
        return path

    def _remap_input(self,operation,path,*args,**kw):
        """Called for path inputs"""
        return self._validate_path(path)

    def _remap_output(self,operation,path):
        """Called for path outputs"""
        return self._validate_path(path)

    def _remap_pair(self,operation,src,dst,*args,**kw):
        """Called for path pairs like rename, link, and symlink operations"""
        return (
            self._remap_input(operation+'-from',src,*args,**kw),
            self._remap_input(operation+'-to',dst,*args,**kw)
        )


if hasattr(os, 'devnull'):
    _EXCEPTIONS = [os.devnull,]
else:
    _EXCEPTIONS = []

try:
    from win32com.client.gencache import GetGeneratePath
    _EXCEPTIONS.append(GetGeneratePath())
    del GetGeneratePath
except ImportError:
    # it appears pywin32 is not installed, so no need to exclude.
    pass

class DirectorySandbox(AbstractSandbox):
    """Restrict operations to a single subdirectory - pseudo-chroot"""

    write_ops = dict.fromkeys([
        "open", "chmod", "chown", "mkdir", "remove", "unlink", "rmdir",
        "utime", "lchown", "chroot", "mkfifo", "mknod", "tempnam",
    ])

    _exception_patterns = [
        # Allow lib2to3 to attempt to save a pickled grammar object (#121)
        '.*lib2to3.*\.pickle$',
    ]
    "exempt writing to paths that match the pattern"

    def __init__(self, sandbox, exceptions=_EXCEPTIONS):
        self._sandbox = os.path.normcase(os.path.realpath(sandbox))
        self._prefix = os.path.join(self._sandbox,'')
        self._exceptions = [
            os.path.normcase(os.path.realpath(path))
            for path in exceptions
        ]
        AbstractSandbox.__init__(self)

    def _violation(self, operation, *args, **kw):
        raise SandboxViolation(operation, args, kw)

    if _file:
        def _file(self, path, mode='r', *args, **kw):
            if mode not in ('r', 'rt', 'rb', 'rU', 'U') and not self._ok(path):
                self._violation("file", path, mode, *args, **kw)
            return _file(path,mode,*args,**kw)

    def _open(self, path, mode='r', *args, **kw):
        if mode not in ('r', 'rt', 'rb', 'rU', 'U') and not self._ok(path):
            self._violation("open", path, mode, *args, **kw)
        return _open(path,mode,*args,**kw)

    def tmpnam(self):
        self._violation("tmpnam")

    def _ok(self, path):
        active = self._active
        try:
            self._active = False
            realpath = os.path.normcase(os.path.realpath(path))
            return (
                self._exempted(realpath)
                or realpath == self._sandbox
                or realpath.startswith(self._prefix)
            )
        finally:
            self._active = active

    def _exempted(self, filepath):
        start_matches = (
            filepath.startswith(exception)
            for exception in self._exceptions
        )
        pattern_matches = (
            re.match(pattern, filepath)
            for pattern in self._exception_patterns
        )
        candidates = itertools.chain(start_matches, pattern_matches)
        return any(candidates)

    def _remap_input(self, operation, path, *args, **kw):
        """Called for path inputs"""
        if operation in self.write_ops and not self._ok(path):
            self._violation(operation, os.path.realpath(path), *args, **kw)
        return path

    def _remap_pair(self, operation, src, dst, *args, **kw):
        """Called for path pairs like rename, link, and symlink operations"""
        if not self._ok(src) or not self._ok(dst):
            self._violation(operation, src, dst, *args, **kw)
        return (src,dst)

    def open(self, file, flags, mode=0o777, *args, **kw):
        """Called for low-level os.open()"""
        if flags & WRITE_FLAGS and not self._ok(file):
            self._violation("os.open", file, flags, mode, *args, **kw)
        return _os.open(file,flags,mode, *args, **kw)

WRITE_FLAGS = functools.reduce(
    operator.or_, [getattr(_os, a, 0) for a in
        "O_WRONLY O_RDWR O_APPEND O_CREAT O_TRUNC O_TEMPORARY".split()]
)

class SandboxViolation(DistutilsError):
    """A setup script attempted to modify the filesystem outside the sandbox"""

    def __str__(self):
        return """SandboxViolation: %s%r %s

The package setup script has attempted to modify files on your system
that are not within the EasyInstall build area, and has been aborted.

This package cannot be safely installed by EasyInstall, and may not
support alternate installation locations even if you run its setup
script by hand.  Please inform the package's author and the EasyInstall
maintainers to find out if a fix or workaround is available.""" % self.args



























#
