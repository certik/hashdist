"""
:mod:`hashdist.core.build_tools` --- Tools to assist build scripts
==================================================================

Reference
---------


"""

import os
from os.path import join as pjoin
import json
from string import Template
from textwrap import dedent
import re

from .common import json_formatting_options
from .build_store import BuildStore
from .profile import make_profile
from .fileutils import rmdir_empty_up_to, write_protect

def execute_files_dsl(files, env):
    """
    Executes the mini-language used in the "files" section of the build-spec.
    See :class:`.BuildWriteFiles`.

    Relative directories in targets are relative to current cwd.

    Parameters
    ----------

    files : json-like
        Files to create in the "files" mini-language

    env : dict
        Environment to use for variable substitutation
    """
    def subs(x):
        return Template(x).substitute(env)
    
    for file_spec in files:
        target = subs(file_spec['target'])
        # Automatically create parent directory of target
        dirname, basename = os.path.split(target)
        if dirname != '' and not os.path.exists(dirname):
            os.makedirs(dirname)

        if sum(['text' in file_spec, 'object' in file_spec]) != 1:
            raise ValueError('objects in files section must contain either "text" or "object"')
        if 'object' in file_spec and 'expandvars' in file_spec:
            raise NotImplementedError('"expandvars" only supported for "text" currently')

        # IIUC in Python 3.3+ one can do exclusive creation with the 'x'
        # file mode, but need to do it ourselves currently
        if file_spec.get('executable', False):
            mode = 0o755
        else:
            mode = 0o644
        fd = os.open(pjoin(dirname, basename), os.O_EXCL | os.O_CREAT | os.O_WRONLY, mode)
        with os.fdopen(fd, 'w') as f:
            if 'text' in file_spec:
                text = os.linesep.join(file_spec['text'])
                if file_spec.get('expandvars', False):
                    text = subs(text)
                f.write(text)
            else:
                json.dump(file_spec['object'], f, **json_formatting_options)

def get_import_envvar(env):
    return env['HDIST_IMPORT'].split()

def build_whitelist(build_store, artifact_ids, stream):
    for artifact_id in artifact_ids:
        path = build_store.resolve(artifact_id)
        if path is None:
            raise Exception("Artifact %s not found" % artifact_id)
        stream.write('%s\n' % pjoin(path, '**'))
        #with open(pjoin(path, 'artifact.json')) as f:
        #    doc = json.load(f)

def recursive_list_files(dir):
    result = set()
    for root, dirs, files in os.walk(dir):
        for fname in files:
            result.add(pjoin(root, fname))
    return result

def push_build_profile(config, logger, virtuals, buildspec_filename, manifest_filename, target_dir):
    files_before_profile = recursive_list_files(target_dir)
    
    with open(buildspec_filename) as f:
        imports = json.load(f).get('build', {}).get('import', [])
    build_store = BuildStore.create_from_config(config, logger)
    make_profile(logger, build_store, imports, target_dir, virtuals, config)

    files_after_profile = recursive_list_files(target_dir)
    installed_files = files_after_profile.difference(files_before_profile)
    with open(manifest_filename, 'w') as f:
        json.dump({'installed-files': sorted(list(installed_files))}, f)

def pop_build_profile(manifest_filename, root):
    with open(manifest_filename) as f:
        installed_files = json.load(f)['installed-files']
    for fname in installed_files:
        os.unlink(fname)
        rmdir_empty_up_to(os.path.dirname(fname), root)


#
# Tools to use on individual files for postprocessing
#

def is_executable(filename):
    return os.stat(filename).st_mode & 0o111 != 0

def postprocess_launcher_shebangs(filename, launcher_program):
    if not os.path.isfile(filename):
        return
    if 'bin' not in filename:
        return
    
    if is_executable(filename):
        with open(filename) as f:
            is_script = (f.read(2) == '#!')
            if is_script:
                script_no_hashexclam = f.read()

    if is_script:
        script_filename = filename + '.real'
        dirname = os.path.dirname(filename)
        rel_launcher = os.path.relpath(launcher_program, dirname)
        # Set up:
        #   thescript      # symlink to ../../path/to/launcher
        #   thescript.real # non-executable script with modified shebang
        lines = script_no_hashexclam.splitlines(True) # keepends=True
        cmd = lines[0].split()
        interpreters = '${PROFILE_BIN_DIR}/%s:${ORIGIN}/%s' % (
            os.path.basename(cmd[0]), os.path.relpath(cmd[0], dirname))
        cmd[0] = interpreters
        lines[0] = '#!%s\n' % ' '.join(cmd)
        with open(script_filename, 'w') as f:
            f.write(''.join(lines))
        write_protect(script_filename)
        os.unlink(filename)
        os.symlink(rel_launcher, filename)

def postprocess_multiline_shebang(filename):
    """
    Try to rewrite the shebang of scripts. This function deals with
    detecting whether the script is a shebang, and if so, rewrite it.
    """
    if not os.path.isfile(filename) or not is_executable(filename):
        return

    with open(filename) as f:
        if f.read(2) != '#!':
            # no shebang
            return

        scriptlines = f.readlines()
        scriptlines[0] = '#!' + scriptlines[0]

    try:
        mod_scriptlines = make_relative_multiline_shebang(filename, scriptlines)
    except UnknownShebangError:
        # just leave unsupported scripts as is
        pass
    else:
        if mod_scriptlines != scriptlines:
            with open(filename, 'w') as f:
                f.write(''.join(mod_scriptlines))
        

def postprocess_write_protect(filename):
    """
    Write protect files. Leave directories alone because the inability
    to rm -rf is very annoying.
    """
    if not os.path.isfile(filename):
        return
    write_protect(filename)


#
# Shebang
#
class UnknownShebangError(NotImplementedError):
    pass


_SYSTEM_INTERPRETER_SHEBANG_RE = re.compile(r'^#!\s+(/bin|/usr/bin)/.*')
def make_relative_multiline_shebang(filename, scriptlines):
    """
    See module docstring for motivation.

    Any shebang starting with "/bin" or "/usr/bin" will be left intact (including
    "/usr/bin/env python"). Otherwise, it is assumed they refer to a
    build artifact by absolute path and we rewrite it to a relative
    one.

    Parameters
    ----------

    scriptlines : list of str
        List of lines in the script; each line includes terminating newline

    Returns
    -------

    scriptlines : list of str
        List of lines of modified script; each line including terminating newline

    """
    shebang = scriptlines[0]
    if _SYSTEM_INTERPRETER_SHEBANG_RE.match(shebang):
        return scriptlines
    elif 'python' in shebang:
        mod_scriptlines = patch_python_shebang(filename, scriptlines)
    else:
        raise UnknownShebangError('No support for shebang "%s" in file "%s"' %
                                  (shebang, filename))
    return mod_scriptlines


# Assume that the script is called through a set of symlinks; p_n ->
# ... -> p_1 -> script. On call $0 is p_n, and we assume the first
# non-symlink is the script. For each link in the chain, we walk .. to
# the root (of the *physical* path) checking for the presence of the
# file "is-profile"; if found, we launch the interpreter from the
# "bin"-directory beneath it. If not found, we use the given relpath
# relative to the script location.

_launcher_script = dedent("""\
    r="%(relpath)s" # relative path to bin-directory if profile lookup fails
    i="%(interpreter)s" # interpreter base-name
    %(arg_assign)s # may be 'arg=...' if there is an argument
    o=`pwd`

    # Loop to follow chain of links by cd-ing to their
    # directories and calling readlink. $p is current link.
    # After exiting loop, one is in the directory of the real script.
    p="$0"
    while true; do
        # Must test for whether $p is a link, and we should continue looping
        # or not, before we change directory.
        test -L "$p"
        il=$? # is_link
        cd `dirname "$p"`
        pdir=`pwd -P`
        d="$pdir"

        # Loop to cd upwards towards root searching for "profile.json" file.
        while [ "$d" != / ]; do
          [ -e profile.json ]&&cd "$o"&&exec "$d/bin/$i" "$0"%(arg_expr)s"$@"
          cd ..
          d=`pwd -P`
        done

        # No is-profile found; 
        cd "$pdir"
        if [ "$il" -ne 0 ];then break;fi
        p=`readlink $p`
    done
    # No profile found, execute relative
    cd "$r"
    p=`pwd -P`

    cd "$o"
    exec "$p/$i" "$0"%(arg_expr)s"$@"
    exit 127
""")

def _get_launcher(script_filename, shebang):
    if shebang[:2] != '#!':
        raise ValueError("expected a shebang first in '%s'" % script_filename)
    cmd = [x.strip() for x in shebang[2:].split()]
    if len(cmd) > 1:
        # in shebangs the remainder is a single argument
        arg_assign = 'arg="%s"' % ' '.join(cmd[1:])
        arg_expr = '"$arg"'
    else:
        arg_assign = arg_expr = ' '
    relpath = os.path.relpath(os.path.dirname(cmd[0]),
                              os.path.dirname(script_filename))
    return _launcher_script % dict(interpreter=os.path.basename(cmd[0]),
                                   arg_assign=arg_assign,
                                   arg_expr=arg_expr,
                                   relpath=relpath)

_CLEAN_RE = re.compile(r'^([^#]*)(#.*)?$')
def pack_sh_script(script):
    lines = script.splitlines()
    lines = [_CLEAN_RE.match(line).group(1).strip() for line in lines]
    lines = [x for x in lines if len(x) > 0]
    lines = [x + ' ' if x.endswith(' do') or x.endswith(' then') else x + ';' for x in lines]
    return ''.join(lines)

_PY_EMPTY_RE = re.compile(r'^\s*(#.*)?$')
_PY_DOCSTR_RE = re.compile(r'^\s*[ubrUBR]+(\'\'\'|""").*$')
def patch_python_shebang(filename, scriptlines):
    """
    Replaces a Python shebang with a multi

    The shebang is *assumed* to be on the form "/absolute/path/to/python";
    the caller should check for forms such as "/usr/bin/env python" first.
    The shebang is replaced with a "multi-line"
    shebang, using the property that::

        #/bin/sh
        "true" '''\' SHELL_SCRIPT
        '''
        PYTHON_SCRIPT

    is both executable by a Unix shell and by Python. The the Unix
    shell script is seen as the module docstring by Python, so we must
    also patch up the module docstring by prepending '__doc__ = '.

    Parameters
    ----------

    filename : str
        Filename of script. Not modified (by this function), but used to
        access the relative path.

    scriptlines : list of str
        List of lines in the script; each line includes terminating newline

    Returns
    -------

    scriptlines : list of str
        List of lines of modified script; each line including terminating newline
    """
    shebang = scriptlines[0]
    del scriptlines[0] # remove old shebang

    # prepend module docstring with "__doc__ = "
    for i, line in enumerate(scriptlines):
        if _PY_DOCSTR_RE.match(line):
            scriptlines[i] = '__doc__ = ' + scriptlines[i]
        if not _PY_EMPTY_RE.match(line):
            break

    launcher_script = _get_launcher(filename, shebang)
    preamble = dedent("""\
    #!/bin/sh
    "true" '''\\';%s
    ''' # end multi-line shebang, see hashdist.core.build_tools
    """) % pack_sh_script(launcher_script)

    lines = preamble.splitlines(True) + scriptlines
    lines = add_modelines(lines, 'python')
    return lines

def add_modelines(scriptlines, language):
    """Sets file metadata/modelines so that editors will treat it correctly

    Since setting the shebang destroys auto-detection for scripts, we add
    mode-lines for Emacs and vi.
    """
    shebang = scriptlines[0:1]
    body = scriptlines[1:]
    if not any('-*-' in line for line in scriptlines):
        emacs_modeline = ['# -*- mode: %s -*-\n' % language]
    else:
        emacs_modeline = []
    if not any(' vi:' in line for line in scriptlines):
        vi_modeline = ['# vi: filetype=python\n']
    else:
        vi_modeline = []
    return shebang + emacs_modeline + body + vi_modeline

    
