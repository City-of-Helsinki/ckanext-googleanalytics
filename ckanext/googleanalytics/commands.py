import os
import re
import logging
import datetime
import time

from pylons import config as pylonsconfig
from ckan.lib.cli import CkanCommand
import ckan.model as model

import ckan.plugins as p
from ckanext.googleanalytics.model import PackageStats,ResourceStats

PACKAGE_URL = '/dataset/'  # XXX get from routes...
DEFAULT_RESOURCE_URL_TAG = '/downloads/'

RESOURCE_URL_REGEX = re.compile('/dataset/[a-z0-9-_]+/resource/([a-z0-9-_]+)')
DATASET_EDIT_REGEX = re.compile('/dataset/edit/([a-z0-9-_]+)')

log = logging.getLogger(__name__)


class InitDB(CkanCommand):
    """Initialise the local stats database tables
    """
    summary = __doc__.split('\n')[0]
    usage = __doc__
    max_args = 0
    min_args = 0

    def command(self):
        self._load_config()
        from ckanext.googleanalytics.model import init_tables
        init_tables(model.meta.engine)
        log.info("Set up statistics tables in main database")


class LoadAnalytics(CkanCommand):
    """Parse data from Google Analytics API and store it
    in a local database

    Options:
        <credentials_file> internal [date] use ckan internal tracking tables
                        credentials_file specifies the OAUTH credentials file
                        date specifies start date for retrieving
                        analytics data YYYY-MM-DD format
    """
    summary = __doc__.split('\n')[0]
    usage = __doc__
    max_args = 2
    min_args = 1
    TEST_HOST = None
    CONFIG = None

    def command(self):
        """
        Parse data from Google Analytics API and store it
        in a local database
        """

        if not self.CONFIG:
            self._load_config()
            self.CONFIG = pylonsconfig

        self.resource_url_tag = self.CONFIG.get(
            'googleanalytics_resource_prefix',
            DEFAULT_RESOURCE_URL_TAG)

        self.parse_and_save()


    def ga_query(self, start_date=None, end_date=None):
        """
        Get raw data from Google Analtyics for packages and
        resources.

        Returns a dictionary like::

           {'identifier': 3}
        """
        start_date = start_date.strftime("%Y-%m-%d")
        if not end_date:
            end_date = datetime.datetime.now()

        end_date = end_date.strftime("%Y-%m-%d")

        query = 'ga:pagePath=~%s,ga:pagePath=~%s' % \
                    (PACKAGE_URL, self.resource_url_tag)
        metrics = 'ga:uniquePageviews'
        sort = '-ga:uniquePageviews'

        start_index = 1
        max_results = 10000

        print '%s -> %s' % (start_date, end_date)
        
        results = self.service.data().ga().get(ids='ga:%s' % self.profile_id,
                                 filters=query,
                                 dimensions='ga:pagePath, ga:date',
                                 start_date=start_date,
                                 end_date=end_date,
                                 start_index=start_index,
                                 max_results=max_results,
                                 metrics=metrics,
                                 sort=sort
                                 ).execute()
        return results    
          
    def parse_and_save(self):
        """Grab raw data from Google Analytics and save to the database"""
        from ga_auth import (init_service, get_profile_id)
        if len(self.args) == 0:
            raise Exception("Missing token file")
        tokenfile = self.args[0]
        if not os.path.exists(tokenfile):
            raise Exception('Cannot find the token file %s' % self.args[1])

        try:
            self.service = init_service(self.args[0])
        except TypeError as e:
            raise Exception('Unable to create a service: {0}'.format(e))

        self.profile_id = get_profile_id(self.service)
        if len(self.args) > 2:
            raise Exception('Too many arguments')

        given_start_date = None
        if len(self.args) == 2:
            given_start_date = datetime.datetime.strptime(self.args[1], '%Y-%m-%d').date()

        packages_data = self.get_ga_data(start_date=given_start_date)
        self.save_ga_data(packages_data)
        log.info("Saved %s records from google" % len(packages_data))

    def save_ga_data(self, packages_data):
        """
        Save tuples of packages_data to the database
        """
        for identifier, visits_collection in packages_data.items():
            visits = visits_collection.get('visits', {})
            matches = RESOURCE_URL_REGEX.match(identifier)      
            if matches:
                resource_url = identifier[len(self.resource_url_tag):]
                resource = model.Session.query(model.Resource).autoflush(True)\
                           .filter_by(id=matches.group(1)).first()
                if not resource:
                    log.warning("Couldn't find resource %s" % resource_url)
                    continue
                for visit_date, count in visits.iteritems():
                    ResourceStats.update_visits(resource.id, visit_date, count)
                    log.info("Updated %s with %s visits" % (resource.id, count))
            else:
                package_name = identifier[len(PACKAGE_URL):]
                if "/" in package_name:
                    log.warning("%s not a valid package name" % package_name)
                    continue
                item = model.Package.by_name(package_name)
                if not item:
                    log.warning("Couldn't find package %s" % package_name)
                    continue
                for visit_date, count in visits.iteritems():
                    PackageStats.update_visits(item.id, visit_date, count)
                    log.info("Updated %s with %s visits" % (item.id, count))
        model.Session.commit()

    def get_ga_data(self, start_date=None):
        """
        Get raw data from Google Analytics for packages and
        resources for the start date given as parameter or last time since database was updated and 2 days more

        Returns a dictionary like::

           {'identifier': {'visits':3, 'visit_date':<time>}}
        """
        now = datetime.datetime.now()

        # If there is no last valid value found from database then we make sure to grab all values from start. i.e. 2014
        # We want to take minimum 2 days worth logs even latest_date is today
        floor_date = datetime.date(2014, 1, 1)

        if start_date is not None:
            floor_date = start_date
        
        latest_date = PackageStats.get_latest_update_date()
        
        if latest_date is not None and start_date is None:
            floor_date = latest_date - datetime.timedelta(days=2)

        packages = {}
        queries = ['ga:pagePath=~%s' % PACKAGE_URL]

        current_month = datetime.date(now.year, now.month, 1)
        dates = []

        #If floor date and current month belong to the same month no need to add backward months
        if current_month != datetime.date(floor_date.year,floor_date.month,1):
            while current_month > datetime.date(floor_date.year, floor_date.month, floor_date.day):
                dates.append(current_month)
                current_month = current_month - datetime.timedelta(days=30)
        dates.append(floor_date)

        current = now
        for date in dates:

            for query in queries:
                results = self.ga_query(start_date=date,
                                        end_date=current)
                if 'rows' in results:
                    for result in results.get('rows'):

                        package = result[0]
                        if not package.startswith(PACKAGE_URL):
                            package = '/' + '/'.join(package.split('/')[2:])
                        if package.startswith('/fi/') or package.startswith('/sv/') or package.startswith('/en/'):
                            package = '/' + '/'.join(package.split('/')[2:])

                        visit_date = datetime.datetime.strptime(result[1], "%Y%m%d").date()
                        count = result[2]
                        # Make sure we add the different representations of the same
                        # dataset /mysite.com & /www.mysite.com ...

                        val = 0
                        if package in packages and "visits" in packages[package]:
                            if visit_date in packages[package]['visits']:
                                val += packages[package]["visits"][visit_date]
                        else:
                            packages.setdefault(package, {})["visits"] = {}
                        packages[package]['visits'][visit_date] =  int(count) + val
            current = date
        return packages
