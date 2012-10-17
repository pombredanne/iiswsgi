#!/usr/bin/env python2.7

import sys
import os
import subprocess
import shutil
import logging
import argparse
import urlparse

from xml.dom import minidom

from iiswsgi import options
from iiswsgi import setup

logger = logging.getLogger('iiswsgi.build')


class Builder(object):
    """
    Helper for building IIS WSGI Web Deploy packages.

    Performs the following tasks: build the Web Deploy Package,
    calculate the size and sha1, delete old Web Deploy packages from
    the Web Platform Installer cache, update the size and sha1 in the
    Web Platform Installer feed, write the Web Platform Installer feed
    to `web-pi.xml`, delete copies of the feed from the Web Platform
    Installer cache, delete `iis_deploy.stamp` files from all
    installations of any of the given packages in
    `%USERPROFILE%\Documents\My Web Sites`
    """

    feed_name = 'web-pi.xml'
    webpi_installer_cache = os.path.join(
        os.environ['LOCALAPPDATA'],
        'Microsoft', 'Web Platform Installer', 'installers')
    iis_sites_home = os.path.join(
        os.environ['USERPROFILE'], 'Documents', 'My Web Sites')
    feed_dir = os.path.join(
        os.environ['LOCALAPPDATA'], 'Microsoft', 'Web Platform Installer')

    def __init__(self, packages, feed=None):
        self.packages = packages
        self.feed = feed
        self.cwd = os.getcwd()

    def __call__(self):
        feed = self.parse_feed()

        for package in self.packages:
            dist, version, package_size, package_sha1 = self.build_package(
                package)
            manifest = minidom.parse(os.path.join(package, 'Manifest.xml'))
            app_name = setup.get_app_name(manifest)
            self.update_feed_entry(
                feed, app_name, dist, version, package_size, package_sha1)
            self.delete_installer_cache(app_name)
            self.delete_stamp_files(app_name)

        self.write_feed(feed)
        self.delete_feed_cache(feed)

    def parse_feed(self):
        if self.feed is None:
            return

        feed = self.feed
        if os.path.exists(feed + '.in'):
            # We have a template
            feed = feed + '.in'
        return minidom.parse(feed).firstNode

    def build_package(self, package):
        try:
            os.chdir(package)
            dist_name, version = subprocess.check_output(
                [sys.executable, 'setup.py', '--name', '--version']).split()
            subprocess.check_call(
                [sys.executable, 'setup.py', 'build', 'sdist', -'q'])
            dist = os.path.abspath(os.path.join('dist', '{0}-{1}.zip'.format(
                dist_name, version)))
            package_size = os.path.getsize(dist)
            package_sha1 = subprocess.check_output(['fciv', '-sha1', dist])
        finally:
            os.chdir(self.cwd)

        package_size = int(round(package_size / 1024.0))
        return dist, version, package_size, package_sha1

    def update_feed_entry(
        self, feed, app_name, dist, version, package_size, package_sha1):
        if feed is None:
            return

        for entry in feed.getElementsByTagName('entry'):
            productIds = entry.getElementsByTagName("productId")
            if productIds and productIds[0].firstChild.data == app_name:
                break
        else:
            raise ValueError(
                'Could not find <entry> for {0}'.format(app_name))

        version_elem = entry.getElementsByTagName('version')[0]
        version_elem.firstChild.data = u'{0}'.format(version)
        logger.info('Set Web Platform Installer <version> to {0}'.format(
            version))

        installer_url = urlparse.urlunsplit((
            'file', '', dist.replace(os.sep, '/'), '', ''))
        installer_elem = entry.getElementsByTagName('installerURL')[0]
        installer_elem.firstChild.data = u'{0}'.format(installer_url)
        logger.info('Set Web Platform Installer <installerURL> to {0}'.format(
            installer_url))

        size_elem = entry.getElementsByTagName('fileSize')[0]
        size_elem.firstChild.data = u'{0}'.format(package_size)
        logger.info('Set Web Platform Installer <fileSize> to {0}'.format(
            package_size))

        package_sha1_value = package_sha1.rsplit(
            '\r\n', 2)[-2].split(' ', 1)[0]
        sha1_elem = entry.getElementsByTagName('sha1')[0]
        sha1_elem.firstChild.data = u'{0}'.format(package_sha1_value)
        logger.info('Set Web Platform Installer <sha1> to {0}'.format(
            package_sha1_value))

    def delete_installer_cache(self, app_name):
        installer_dir = os.path.join(self.webpi_installer_cache, app_name)
        if os.path.exists(installer_dir):
            logger.info('Removing the cached MSDeploy package: {0}'.format(
                installer_dir))
            shutil.rmtree(installer_dir)

    def delete_stamp_files(self, app_name):
        # Clean up likely stale stamp files
        for name in os.listdir(self.iis_sites_home):
            if not (os.path.isdir(os.path.join(self.iis_sites_home, name)) and
                    name.startswith(app_name)):
                continue
            stamp_file = os.path.join(
                self.iis_sites_home, name, 'iis_deploy.stamp')
            if os.path.exists(stamp_file):
                logger.info(
                    'Removing stale deploy stamp file: {0}'.format(stamp_file))
                os.remove(stamp_file)

    def write_feed(self, feed):
        if feed is None:
            return

        logger.info('Writing Web Platform Installer feed to {0}'.format(
            self.feed))
        feed.writexml(open(self.feed, 'w'))

    def delete_feed_cache(self, feed):
        if feed is None:
            return

        for cached_feed_name in os.listdir(self.feed_dir):
            if not os.path.splitext(cached_feed_name)[1] == '.xml':
                # not a cached feed file
                continue

            cached_feed_path = os.path.join(self.feed_dir, cached_feed_name)
            cached_feed = minidom.parse(cached_feed_path).firstNode
            # TODO Assumes that the first <id> element is the feed/id
            # Would not be true if an entry/id came before the feed/id
            ids = cached_feed.getElementsByTagName("id")
            if ids and (ids[0].firstChild.data ==
                        feed.getElementsByTagName("id")[0].firstChild.data):
                logger.info(
                    'Removing the Web Platform Installer cached feed at {0}'
                    .format(cached_feed_path))
                os.remove(cached_feed_path)
                break


build_parser = argparse.ArgumentParser(description=Builder.__doc__,
                                     parents=[options.parent_parser])
build_parser.add_argument('-f', '--feed',
                          help="""\
Web Platform Installer atom feed to update.  If a file of the same name but \
with a `*.in` extension exists it will be used as a template.  \
Useful to avoid versioning irrellevant feed changes.""")
build_parser.add_argument('packages', nargs='+',
                          help="""\
One or more Web Deploy package directories.  Each must contain `setup.py` \
files which use the `iiswsgi.setup` `distutils` commands to \
generate a package.""")


def build_console(args=None):
    logging.basicConfig()
    args = build_parser.parse_args(args=args)
    builder = Builder(args.packages, feed=args.feed)
    builder()
