"""
Run post-install tasks for a MS Web Deploy package:

2. write variable substitutions into `web.config`

3. install an IIS FastCGI application

4. set up a `virtualenv` isolated Python environment

5. install requirements with `pip` or `easy_install`

7. test the IIS WSGI app

Where possible, automatic detection is used when deciding whether to
run a given task.
"""

import os
import sys
import subprocess
import argparse
import logging
import re

from xml.dom import minidom

from distutils import cmd
from distutils.command import install

from iiswsgi import options
from iiswsgi import fcgi
from iiswsgi import build_msdeploy

root = logging.getLogger()
logger = logging.getLogger('iiswsgi.install')

# Default to running this command: ['install_msdeploy']
command = __name__.rsplit('.', 1)[1]
setup_args = [command]


class install_msdeploy(cmd.Command):
    # From module docstring
    description = __doc__ = __doc__

    user_options = [
        ('skip-fcgi-app-install', 's', """\
Run the install process even if the `iis_install.stamp` file is not present.  \
This can be usefule to manually re-run the deployment after an error that \
stopped a previous run has been addressed."""),
        ('requirements-filename=', 'r',
         "Path to a pip requirements file to install into a virtualenv."),
        ('easy-install-filename=', 'e', """\
Path to file with one easy_install requirement per line install into a \
virtualenv."""),
        ('find-links=', 'f',
         "Additional find_links for easy_install and pip")]

    logger = logger

    def initialize_options(self):
        self.skip_fcgi_app_install = False
        self.requirements_filename = 'requirements.txt'
        self.easy_install_filename = 'easy_install.txt'
        self.executable = sys.executable
        self.find_links = None
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

        self.ensure_string_list('find_links')

    def run(self):
        """
        Run all deployment tasks and a custom script as appropriate.

        * `self.install()`: perform tasks as appropriate

        * `self.test()`: test the WSGI application and FCGI server

        To excercise custom control over installation, override this
        method in a subclass and use:

            setup(...
                cmdclass=dict(install_msdeploy=<install_msdeploy_subclass>)...
        """
        self.install()
        self.test()

    def install(self, *requirements, **substitutions):
        """
        Perform all of the deployment tasks as appropriate.

        `self.write_web_config()`:

            Write variable substitutions into `web.config`.

        `iiswsgi.fcgi.install_fcgi_app()`:

            Install an IIS FastCGI application.

        `self.setup_virtualenv()`:

            If `APPL_PHYSICAL_PATH` has a `requirements.txt` and/or
            `easy_install.txt` file then a `virtualenv` will be setup
            to provide an isolated Python environment.

        `self.pip_install_requirements()`, `self.easy_install_requirements()`:

            Use `pip` or `easy_install` to install requirements into
            the `virtualenv`.
        """
        # TODO move to `setup.py install`?
        self.write_web_config(**substitutions)

        if not self.skip_fcgi_app_install:
            fcgi.install_fcgi_app()

        # vritualenv and requirements
        if (os.path.exists(self.requirements_filename) or
            os.path.exists(self.easy_install_filename)):
            self.executable = self.setup_virtualenv()

            if os.path.exists(self.requirements_filename):
                self.pip_install_requirements()

            if os.path.exists(self.easy_install_filename):
                self.easy_install_requirements(*requirements)

    def write_web_config(self, **kw):
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
        environ = os.environ.copy()
        environ.update(**kw)
        web_config = open('web.config.in').read()
        self.logger.info('Doing variable substitution in web.config')
        open('web.config', 'w').write(web_config.format(**environ))
        return environ

    def setup_virtualenv(self, directory='.', **opts):
        """
        Set up a virtualenv in the `directory` with options.
        """
        cmd = [options.get_script_path('virtualenv', self.executable)]
        for option, value in opts.iteritems():
            cmd.extend(['--' + option, value])
        cmd.extend([directory])
        self.logger.info(
            'Setting up a isolated Python with: {0}'.format(
                ' '.join(cmd)))
        subprocess.check_call(cmd, env=os.environ)
        return os.path.abspath(
            os.path.join(options.scripts_name, 'python' + options.script_ext))

    def pip_install_requirements(
        self, filename=None, requirements=(),
        find_links=None):
        """Use pip to install requirements from the given file."""
        if not filename and not requirements:
            filename = self.requirements_filename
        cmd = [os.path.abspath(options.get_script_path(
            'pip', self.executable))]
        self._add_find_links(cmd, find_links)
        cmd.extend(['install'])
        if filename:
            cmd.extend(['-r', filename])
        if requirements:
            cmd.extend(requirements)
        self.logger.info(
            'Installing dependencies with pip: {0}'.format(
                ' '.join(cmd)))
        subprocess.check_call(cmd, env=os.environ)

    def easy_install_requirements(
        self, filename=None, requirements=(), find_links=None):
        """
        Use easy_install to install requirements.

        The requiremensts can be given as arguments or one per-line in
        the `filename`.
        """
        if not filename and not requirements:
            filename = self.easy_install_filename
        cmd = [os.path.abspath(
            options.get_script_path('easy_install', self.executable))]
        self._add_find_links(cmd, find_links)
        if filename:
            cmd.extend([line.strip() for line in open(filename)])
        if requirements:
            cmd.extend(requirements)
        self.logger.info(
            'Installing dependencies with easy_install: {0}'
            .format(' '.join(cmd)))
        subprocess.check_call(cmd, env=os.environ)

    def _add_find_links(self, cmd, find_links=None):
        if find_links is None:
            find_links = ()
            if self.find_links:
                find_links = self.find_links
        if isinstance(find_links, str):
            find_links = (find_links, )
        cmd.extend('--find-links=' + find_link for find_link in find_links)
        return cmd

    def test(self):
        """Test the WSGI application and FCGI server."""
        web_config = minidom.parse('web.config')
        for handler in web_config.getElementsByTagName("handlers"):
            for add in handler.getElementsByTagName("add"):
                fullPath, arguments = add.getAttribute(
                    'scriptProcessor').split('|', 1)
                cmd = '"{0}" {1} --test'.format(fullPath, arguments)
                if not os.path.exists(fullPath):
                    logger.error(
                        'IIS FCGI application fullPath does not exist: {0}'
                        .format(cmd))
                    return
                logger.info('Testing the WSGI app: {0}'.format(cmd))
                subprocess.check_call(cmd, shell=True)


def has_msdeploy_manifest(self):
    return os.path.exists(build_msdeploy.manifest_filename)

install.install.sub_commands.append(
    ('install_msdeploy', has_msdeploy_manifest))


class Installer(object):
    """
    Find the APPL_PHYSICAL_PATH and run setup.py there.

    Any additional arguments are passed as arguments to the setup.py
    script.  If there are None, then the default args are '{0}'.
    """.format(' '.join(setup_args))

    logger = logger
    app_name_pattern = '^{0}[0-9]*$'
    stamp_filename = build_msdeploy.stamp_filename

    def __init__(self, app_name=None, require_stamp=True,
                 install_fcgi_app=True):
        self.app_name = app_name
        if app_name:
            self.app_name_pattern = re.compile(
                self.app_name_pattern.format(app_name))
        self.require_stamp = require_stamp

        self.executable = sys.executable

    def __call__(self, setup_args=setup_args):
        appl_physical_path = self.get_appl_physical_path()
        if 'APPL_PHYSICAL_PATH' not in os.environ:
            os.environ['APPL_PHYSICAL_PATH'] = appl_physical_path

        stamp_path = os.path.join(appl_physical_path, self.stamp_filename)
        if os.path.exists(stamp_path):
            # clean up the stamp file regardless, we tried
            os.remove(stamp_path)
        elif self.require_stamp:
            raise ValueError(
                'No IIS install stamp file found at {0}'.format(stamp_path))

        cwd = os.getcwd()
        try:
            self.logger.info('Changing to application directory {0}'.format(
                appl_physical_path))
            os.chdir(appl_physical_path)

            cmd = [sys.executable, 'setup.py'] + setup_args
            self.logger.info('Installing aplication: {0}'.format(
                ' '.join(cmd)))
            subprocess.check_call(cmd)
        finally:
            os.chdir(cwd)

    def get_appl_physical_path(self):
        """
        Finding the `APPL_PHYSICAL_PATH`.

        If already defined, its value is taken as the location of the
        IIS application.  If not attempt to infer the appropriate
        directory.  Until such a time as Web Platform Installer or Web
        Deploy provide some way to identify the physical path of the
        `iisApp` being installed when the `runCommand` provider is
        used, we have to guess at the physical path.  If
        `IIS_SITES_HOME` is defined, all directories that are direct
        children of the `IIS_SITES_HOME` will be searched for a
        `iis_install.stamp` file.  If multiple directories are found
        with the stamp file, an error is raised.  Otherwise, in the
        case where one directory has the stamp file, it is assumed to
        be the `APPL_PHYSICAL_PATH`.

        When installing to "IIS Express", the `IIS_SITES_HOME`
        environment variable should be available and the stamp file
        search should succeed to automatically find the right app for
        which to run setup.py.  In the case of installing to full
        "IIS", however, neither the `APPL_PHYSICAL_PATH` nor the
        `IIS_SITES_HOME` environment variables are available and the
        setup.py won't be run and WebPI will report an error.  The
        best way to workaround this limitation is to adopt a
        convention of putting all your IIS apps installed via WebPI in
        one directory and then set the `IIS_SITES_HOME` enviornment
        variable.  Then when installing a new IIS app be sure to give
        a physical path within that directory when prompted to by
        WebPI.  If that's not possible you can set the
        `APPL_PHYSICAL_PATH` environment variable to the physical path
        you will enter when installing via WebPI. Otherwise, when
        installing to full "IIS" you'll have to follow the steps for
        manually running setup.py after you get the error.
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

        iis_sites_home = os.environ.get('IIS_SITES_HOME')
        if iis_sites_home is None:
            raise ValueError(
                'Neither the APPL_PHYSICAL_PATH nor the IIS_SITES_HOME '
                'environment variables are set.')
        elif not os.path.exists(iis_sites_home):
            raise ValueError(
                ('The IIS_SITES_HOME environment variable value is a '
                 'non-existent path: {0}').format(iis_sites_home))
        elif not os.path.isdir(iis_sites_home):
            raise ValueError(
                ('The IIS_SITES_HOME environment variable value is '
                 'not a directory: {0}').format(iis_sites_home))

        self.logger.info(
            'Searching the directory in the IIS_SITES_HOME environment '
            'variable for the app to install')
        appl_physical_paths = [
            os.path.join(iis_sites_home, name)
            for name in os.listdir(iis_sites_home)
            if self._is_appl_physical_path(iis_sites_home, name)]
        if not appl_physical_paths:
            raise ValueError(
                ('Found no {0} stamp file in any of the directories in the '
                 'IIS_SITES_HOME environment variable: {0}').format(
                    self.stamp_filename, iis_sites_home))
        elif len(appl_physical_paths) > 1:
            raise ValueError(
                ('Found multiple {0} stamp files in the directories in the '
                 'IIS_SITES_HOME environment variable: {0}').format(
                    self.stamp_filename, iis_sites_home))

        appl_physical_path = appl_physical_paths[0]
        self.logger.info(
            ('Found IIS app with a stamp file in just one of the directories '
             'in the IIS_SITES_HOME environment variable: {0}').format(
                    appl_physical_path))

        return appl_physical_path

    def _is_appl_physical_path(self, iis_sites_home, name):
        if self.app_name:
            if self.app_name_pattern.match(name) is None:
                return False
        return (os.path.isdir(os.path.join(iis_sites_home, name))
                and os.path.exists(os.path.join(
                    iis_sites_home, name, self.stamp_filename)))


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
install_console_parser = argparse.ArgumentParser(
    description=Installer.__doc__,
    epilog=Installer.get_appl_physical_path.__doc__,
    parents=[options.parent_parser, install_parser],
    formatter_class=argparse.RawDescriptionHelpFormatter)


def install_console(args=None):
    logging.basicConfig()
    args, setup_args = install_console_parser.parse_known_args(args=args)
    installer = Installer(args.app_name, args.require_stamp)
    installer(setup_args)
