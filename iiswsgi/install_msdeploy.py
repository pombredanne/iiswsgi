"""Run post-install tasks for a MS Web Deploy package."""

# TODO uninstall fastCgi apps

import sys
import os
import subprocess
import argparse
import logging
import re
import sysconfig

from xml.dom import minidom

import distutils.sysconfig
from distutils import errors
from distutils import core
from distutils import cmd

from iiswsgi import options
from iiswsgi import fcgi

root = logging.getLogger()
logger = logging.getLogger('iiswsgi.install')

# Default to running this command: ['install_msdeploy']
command = __name__.rsplit('.', 1)[1]
setup_args = [command]


class install_msdeploy(cmd.Command):
    # From module docstring
    description = __doc__ = __doc__

    user_options = [('skip-fcgi-app-install', 'S',
                     "Do not install IIS FCGI apps.")]

    logger = logger

    def initialize_options(self):
        self.skip_fcgi_app_install = False
        self.app_name_pattern = re.compile(r'^(.*?)([0-9]*)$')

    def finalize_options(self):
        # Configure logging
        build = self.distribution.get_command_obj('build_msdeploy')
        build.ensure_finalized()

        cwd = os.getcwd()
        if 'APPL_PHYSICAL_PATH' not in os.environ:
            os.environ['APPL_PHYSICAL_PATH'] = cwd

        count = self.app_name_pattern.match(cwd).group(2)
        if count:
            self.count = int(count)
        else:
            self.count = 0

        options.ensure_verbosity(self)

    def run(self):
        """
        Run all post-install deployment tasks as appropriate.

        * `self.install()`: perform tasks as appropriate

        * `self.test()`: test the WSGI application and FCGI server

        To excercise custom control over installation, override this
        method in a subclass and use:

            setup(...
                cmdclass=dict(install_msdeploy=<install_msdeploy_subclass>)...
        """
        self.install()
        self.test()

    def install(self, *requirements):
        """
        Set up the app to a point whee it can be tested:

        `setyp.py develop`:

            Install any requirements using easy_install.

        `self.write_web_config()`:

            Write variable substitutions into `web.config`.

        `iiswsgi.fcgi.install_fcgi_app()`:

            Install an IIS FastCGI application.
        """
        self.run_command('develop')

        self.write_web_config()

        if not self.skip_fcgi_app_install:
            fcgi.install_fcgi_app()

    def write_web_config(self):
        """
        Write `web.config.in` to `web.config` substituting variables.

        Substitute environment variables overridden by the kwargs
        using the Python Format String Syntax:

        http://docs.python.org/library/string.html#formatstrings

        This is probably most useful to substitute APPL_PHYSICAL_PATH
        to make sure that each app gets unique IIS FastCGI application
        handlers that can each have their own parameters.  If your
        deployment requires that computed values be included in the
        substituted variables, then use the `--delegate` option and
        pass kwargs into `Installer.install()`.
        """
        web_config = open('web.config.in').read()
        self.logger.info('Doing variable substitution in web.config')
        open('web.config', 'w').write(os.path.expandvars(web_config))
        return web_config

    def test(self):
        """Test the WSGI application and FCGI server."""
        web_config = minidom.parse('web.config')
        for handler in web_config.getElementsByTagName("handlers"):
            for add in handler.getElementsByTagName("add"):
                fullPath, arguments = add.getAttribute(
                    'scriptProcessor').split('|', 1)
                cmd = '"{0}" {1} --test'.format(fullPath, arguments)
                logger.info('Testing the WSGI app: {0}'.format(cmd))
                try:
                    subprocess.check_call(cmd, shell=True)
                except subprocess.CalledProcessError, exc:
                    if exc.returncode == 127:
                        logger.exception(
                            'FCGI app scriptProcessor not found: {0}'
                            .format(cmd))


def has_msdeploy_manifest(self):
    cmd = self.distribution.get_command_obj('build_msdeploy')
    cmd.ensure_finalized()
    return os.path.exists(cmd.manifest_filename)


class Installer(object):
    """
    Find the APPL_PHYSICAL_PATH and run setup.py there.

    Any additional arguments are passed as arguments to the setup.py
    script.  If there are None, then the default args are '{0}'.
    """.format(' '.join(setup_args))

    logger = logger
    stamp_filename = options.stamp_filename

    def __init__(self, app_name=None, require_stamp=True,
                 install_fcgi_app=True, virtualenv=None):
        self.app_name = app_name
        self.require_stamp = require_stamp
        self.virtualenv = virtualenv

    def __call__(self, setup_args=setup_args):
        appl_physical_path = self.get_appl_physical_path()
        if 'APPL_PHYSICAL_PATH' not in os.environ:
            os.environ['APPL_PHYSICAL_PATH'] = str(appl_physical_path)

        stamp_path = os.path.join(appl_physical_path, self.stamp_filename)
        if os.path.exists(stamp_path):
            # clean up the stamp file regardless, we tried
            os.remove(stamp_path)
        elif self.require_stamp:
            raise ValueError(
                'No IIS install stamp file found at {0}'.format(stamp_path))

        cwd = os.getcwd()
        try:
            os.chdir(appl_physical_path)
            if self.virtualenv:
                bootstrap = None
                if self.virtualenv is not None:
                    bootstrap = self.virtualenv
                executable = self.setup_virtualenv(bootstrap=bootstrap)
                cmd = [executable, 'setup.py'] + setup_args
                self.logger.info('Installing aplication: {0}'.format(
                    ' '.join(cmd)))
                return subprocess.check_call(cmd)
            self.logger.info('Installing aplication: setup.py {0}'.format(
                ' '.join(setup_args)))
            return core.run_setup('setup.py', script_args=setup_args)
        finally:
            os.chdir(cwd)

    def get_appl_physical_path(self, appcmd_exe=None):
        """
        Finding the `APPL_PHYSICAL_PATH`.

        If already defined, its value is taken as the location of the
        IIS application.  If not attempt to infer the appropriate
        directory.  Until such a time as Web Platform Installer or Web
        Deploy provide some way to identify the physical path of the
        `iisApp` being installed when the `runCommand` provider is
        used, we have to guess the physical path.

        Start by querying appcmd.exe for all
        sites/site/application/virtualDirectory/@physicalPath
        definitions whose sites/site/@name matches the app name if
        given.  The first such physicalPath with a stamp file is taken
        to be the APPL_PHYSICAL_PATH.
        """
        appl_physical_path = os.environ.get('APPL_PHYSICAL_PATH')
        if appl_physical_path is not None:
            if not os.path.exists(appl_physical_path):
                raise ValueError(
                    ('The APPL_PHYSICAL_PATH environment variable value is a '
                     'non-existent path: {0}').format(appl_physical_path))
            else:
                self.logger.info(
                    ('Found IIS app in APPL_PHYSICAL_PATH environment '
                     'variable at {0}').format(appl_physical_path))
                return appl_physical_path
        else:
            self.logger.info(
                'APPL_PHYSICAL_PATH environment variable not set')

        appl_physical_paths = list(fcgi.list_stamp_paths(
            self.app_name, self.stamp_filename, appcmd_exe))
        if not appl_physical_paths:
            if os.path.exists('setup.py'):
                # maybe the current directory is the path
                dist = core.run_setup('setup.py', stop_after='commandline')
                if dist.get_name() == self.app_name:
                    appl_physical_path = os.getcwd()
            else:
                raise ValueError(
                    ('Found no {0} stamp file in any of the virtual '
                     'directories returned by appcmd.exe').format(
                        self.stamp_filename))
        elif len(appl_physical_paths) > 1:
            appl_physical_path = appl_physical_paths[0]
            logger.error(
                ('Found multiple {0} stamp files in the virtual directories, '
                 '{1}.  Choosing the most recent one: {2}').format(
                    self.stamp_filename, appl_physical_paths[1:],
                    appl_physical_path))
        else:
            appl_physical_path = appl_physical_paths[0]
            self.logger.info(
                ('Found just one IIS app with a stamp file: {0}'
                 ).format(appl_physical_path))

        return appl_physical_path

    def setup_virtualenv(self, home_dir=os.curdir, bootstrap=None, **opts):
        """
        Set up a virtualenv in the `directory` with options.

        If a `bootstrap` file is provided or the `virtualenv_script`
        exists, it is run as a script with positional `args` inserted
        into `sys.argv`.  Otherwise, `virtualenv` is imported and
        `create_environment()` is called with any kwargs.

        Following the run of this command, dependencies can
        automatically be installed with the develop command.
        """
        if bootstrap is None and os.path.exists(self.virtualenv_script):
            bootstrap = self.virtualenv_script

        if bootstrap:
            virtualenv_globals = dict(__file__=bootstrap)
            execfile(bootstrap, virtualenv_globals)

            argv = [bootstrap]
            if self.verbose == 0:
                argv.append('--quiet')
            elif self.verbose == 2:
                argv.append('--verbose')
            for option, value in opts.iteritems():
                argv.extend(['--' + option, value])
            argv.append(home_dir)

            self.logger.info(
                'Setting up a isolated Python with bootstrap script: {0}'
                .format(' '.join(argv)))
            orig_argv = sys.argv[:]
            try:
                sys.argv[:] = argv
                virtualenv_globals['main']()
            finally:
                sys.argv[:] = orig_argv
        else:
            try:
                import virtualenv
            except ImportError:
                raise errors.DistutilsModuleError(
                    'The virtualenv module must be available if no virtualenv '
                    'bootstrap script is given: {0}'.format(bootstrap))
            self.logger.info(
                'Setting up a isolated Python with module: '
                '{0}.create_environment({1} {2})'.format(
                    virtualenv, repr(home_dir), ' '.join(
                        '{0}={1}'.format(item) for item in opts.items())))
            virtualenv.logger = virtualenv.Logger([(
                virtualenv.Logger.level_for_integer(2 - self.verbose),
                sys.stdout)])

            virtualenv.create_environment(home_dir, **opts)

        return os.path.join(sysconfig.get_path('scripts'),
                            'python' + sysconfig.get_config_var('EXE'))

install_parser = argparse.ArgumentParser(add_help=False)
install_parser.add_argument(
    '-a', '--app-name', help="""\
When APPL_PHYSICAL_PATH is not set, narrow the search \
in IIS_SITES_HOME to apps with this name .""")
install_parser.add_argument(
    '-i', '--ignore-stamp', dest='require_stamp', action='store_false',
    help="""\
Run the install process even if the `iis_install.stamp` file is not present.  \
This can be usefule to manually re-run the deployment after an error that \
stopped a previous run has been addressed.""")
install_parser.add_argument(
    '-e', '--virtualenv', nargs="?", const=True, help="""\
Set up a virtualenv.  If an arg is given, use it as a bootstrap script.""")
install_console_parser = argparse.ArgumentParser(
    description=Installer.__doc__,
    epilog=Installer.get_appl_physical_path.__doc__,
    parents=[options.parent_parser, install_parser],
    formatter_class=argparse.RawDescriptionHelpFormatter)


def install_console(args=None):
    logging.basicConfig()
    setup = setup_args
    args, unknown = install_console_parser.parse_known_args(args=args)
    if unknown:
        setup = unknown
    installer = Installer(**vars(args))
    installer(setup)
