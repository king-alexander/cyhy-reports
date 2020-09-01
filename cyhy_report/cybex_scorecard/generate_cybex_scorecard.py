#!/usr/bin/env python

'''Create Cyber Hygiene Cyber Exposure Scorecard PDF.

Usage:
  cyhy-cybex-scorecard --generate-empty-scorecard-json
  cyhy-cybex-scorecard [options] CYHY_DB_SECTION SCAN_DB_SECTION PREVIOUS_SCORECARD_JSON_FILE
  cyhy-cybex-scorecard (-h | --help)
  cyhy-cybex-scorecard --version

Options:
  -a --anonymize                 Make a sample anonymous scorecard.
  -d --debug                     Keep intermediate files for debugging.
  -f --final                     Remove draft watermark.
  -h --help                      Show this screen.
  -n --nolog                     Do not log that this report was created.
  --version                      Show version.

CYHY_DB_SECTION and SCAN_DB_SECTION refer to sections
within /etc/cyhy/cyhy.conf
'''

# standard python libraries
import sys
import os
import copy
from datetime import timedelta
from dateutil import parser
import json
import codecs
import tempfile
import shutil
import subprocess
import csv
from collections import defaultdict
import random

# third-party libraries (install with pip)
import pystache
from pandas import Series, DataFrame
import pandas as pd
import numpy as np
from bson import ObjectId
from docopt import docopt
import requests

# intra-project modules
from cyhy.core import *
from cyhy.util import *
from cyhy.db import database, scheduler
import graphs

# constants
SCORING_ENGINE_VERSION = '1.0'

# Number of days in the past that an org's CyHy tally doc was last changed;
# a.k.a. a 'currently-scanned' org
CURRENTLY_SCANNED_DAYS = 14

CRITICAL_SEVERITY = 4
HIGH_SEVERITY = 3

# Number of days a vulnerability can be active until it's considered
# "overdue" to be mitigated
DAYS_UNTIL_OVERDUE_CRITICAL = 15
DAYS_UNTIL_OVERDUE_HIGH = 30

# Do not include the orgs below (based on _id) in the Scorecard
EXEMPT_ORGS = []

MUSTACHE_FILE = 'cybex_scorecard.mustache'
REPORT_JSON = 'cybex_scorecard.json'
REPORT_PDF = 'cybex_scorecard.pdf'
REPORT_TEX = 'cybex_scorecard.tex'
ASSETS_DIR_SRC = '../assets'
ASSETS_DIR_DST = 'assets'
LATEX_ESCAPE_MAP = {
    '$': '\\$',
    '%': '\\%',
    '&': '\\&',
    '#': '\\#',
    '_': '\\_',
    '{': '\\{',
    '}': '\\}',
    '[': '{[}',
    ']': '{]}',
    "'": "{'}",
    '\\': '\\textbackslash{}',
    '~': '\\textasciitilde{}',
    '<': '\\textless{}',
    '>': '\\textgreater{}',
    '^': '\\textasciicircum{}',
    '`': '{}`',
    '\n': '\\newline{}',
}

ED1901_RESULTS_BY_AGENCY_CSV_FILE = 'cybex-certs-by-agency.csv'
EMAIL_SECURITY_SUMMARY_CSV_FILE = 'cybex-email-security-summary.csv'
BOD_RESULTS_BY_AGENCY_CSV_FILE = 'cybex-bod-results-by-agency.csv'
BOD_1902_RESULTS_BY_AGENCY_CSV_FILE = 'cybex-vuln-results-by-agency.csv'
WEB_SECURITY_RESULTS_BY_AGENCY_CSV_FILE = \
    'cybex-web-security-results-by-agency.csv'
EMAIL_SECURITY_RESULTS_BY_AGENCY_CSV_FILE = \
    'cybex-email-security-results-by-agency.csv'

# Number of Trustymail scans to fetch summary data for
TRUSTYMAIL_SUMMARY_SCAN_DATE_COUNT = 10
BOD1801_DMARC_RUA_URI = 'mailto:reports@dmarc.cyber.dhs.gov'

OCSP_URL = 'https://raw.githubusercontent.com/GSA/data/master/dotgov-websites/ocsp-crl.csv'
OCSP_FILE = '/tmp/ocsp-crl.csv'

TRIPLE_DES_EXCEPTIONS_URL = \
    'https://raw.githubusercontent.com/cisagov/scan-target-data/develop/3des-exception-agencies.csv'
TRIPLE_DES_EXCEPTIONS_FILE = '/tmp/3des-exception-agencies.csv'


class ScorecardGenerator(object):
    def __init__(self, cyhy_db, scan_db, ocsp_file, triple_des_exceptions_file,
                 previous_scorecard_json_file, debug=False, final=False,
                 log_scorecard=True, anonymize=False):
        self.__cyhy_db = cyhy_db
        self.__scan_db = scan_db
        self.__generated_time = utcnow()
        self.__results = dict()  # reusable query results
        self.__requests = None
        self.__tallies = []
        self.__all_cybex_orgs_with_descendants = []
        self.__debug = debug
        self.__draft = not final
        self.__scorecard_doc = {'scores': []}
        self.__cfo_act_orgs = []
        self.__orgs_with_recently_issued_certs = []
        self.__orgs_with_no_recently_issued_certs = []
        self.__orgs_with_no_known_domains = []
        self.__orgs_with_criticals = []
        self.__orgs_with_highs = []
        self.__orgs_without_criticals_or_highs = []
        self.__orgs_not_vuln_scanned = []
        self.__strong_hsts_all = []
        self.__strong_hsts_some = []
        self.__strong_hsts_none = []
        self.__dmarc_reject_all = []
        self.__dmarc_reject_some = []
        self.__dmarc_reject_none = []
        self.__previous_scorecard_data = json.load(codecs.open(
                                            previous_scorecard_json_file, 'r',
                                            encoding='utf-8'))
        self.__scorecard_oid = ObjectId()
        self.__log_scorecard_to_db = log_scorecard
        self.__anonymize = anonymize

        # Read in and parse the OCSP exclusion domains and CISA 3DES
        # exception domains.
        #
        # We use a dict for __ocsp_exclusions and __3des_exceptions
        # because we want to take advantage of the speed of the
        # underlying hash map.  (We only care if a domain/agency is
        # present as an exclusion/exception or not.)
        self.__ocsp_exclusions = {}
        with open(ocsp_file, 'r') as f:
            csvreader = csv.reader(f)
            self.__ocsp_exclusions = {row[0]: None for row in csvreader}

        self.__3des_exceptions = {}
        with open(triple_des_exceptions_file, 'r') as f:
            dictreader = csv.DictReader(f)
            self.__3des_exceptions = {
                # The "3DES Exception" column either contains the
                # string "TRUE" or is empty.
                row['acronym']: None
                for row in dictreader
                if row['3DES Exception'] == "TRUE"
            }

    def __open_tix_opened_in_date_range_pl(self, severity, current_date,
                                           days_until_tix_overdue):
        return [
            {
                '$match': {
                    'open': True,
                    'details.severity': severity,
                    'false_positive': False
                }
            },
            {
                '$group': {
                    '_id': {'owner': '$owner'},
                    'open_tix_opened_less_than_7_days_ago': {
                        '$sum': {
                            '$cond': [
                                {
                                    '$gte': [
                                        '$time_opened',
                                        current_date - timedelta(days=7)
                                    ]
                                },
                                1,
                                0
                            ]
                        }
                    },
                    'open_tix_opened_7-15_days_ago': {
                        '$sum': {
                            '$cond': [
                                {
                                    '$and': [
                                        {
                                            '$lt': [
                                                '$time_opened',
                                                current_date - timedelta(days=7)
                                            ]
                                        },
                                        {
                                            '$gte': [
                                                '$time_opened',
                                                current_date - timedelta(days=15)
                                            ]
                                        }
                                    ]
                                },
                                1,
                                0
                            ]
                        }
                    },
                    'open_tix_opened_15-30_days_ago': {
                        '$sum': {
                            '$cond': [
                                {
                                    '$and': [
                                        {
                                            '$lt': [
                                                '$time_opened',
                                                current_date - timedelta(days=15)
                                            ]
                                        },
                                        {
                                            '$gte': [
                                                '$time_opened',
                                                current_date - timedelta(days=30)
                                            ]
                                        }
                                    ]
                                },
                                1,
                                0
                            ]
                        }
                    },
                    'open_tix_opened_30-90_days_ago': {
                        '$sum': {
                            '$cond': [
                                {
                                    '$and': [
                                        {
                                            '$lt': [
                                                '$time_opened',
                                                current_date - timedelta(days=30)
                                            ]
                                        },
                                        {
                                            '$gte': [
                                                '$time_opened',
                                                current_date - timedelta(days=90)
                                            ]
                                        }
                                    ]
                                },
                                1,
                                0
                            ]
                        }
                    },
                    'open_tix_opened_more_than_90_days_ago': {
                        '$sum': {
                            '$cond': [
                                {
                                    '$lt': [
                                        '$time_opened',
                                        current_date - timedelta(days=90)
                                    ]
                                },
                                1,
                                0
                            ]
                        }
                    },
                    'open_overdue_tix': {
                        '$sum': {
                            '$cond': [
                                {
                                    '$lt': [
                                        '$time_opened',
                                        current_date - timedelta(days=days_until_tix_overdue)
                                    ]
                                },
                                1,
                                0
                            ]
                        }
                    },
                    'open_tix_count': {'$sum': 1}
                }
            }
        ], database.TICKET_COLLECTION

    def __open_tix_opened_in_date_range_for_orgs_pl(self, severity,
                                                    current_date, parent_org,
                                                    descendant_orgs,
                                                    days_until_tix_overdue):
        return [
            {
                '$match': {
                    'open': True,
                    'details.severity': severity,
                    'false_positive': False,
                    'owner': {
                        '$in': [parent_org] + descendant_orgs
                    }
                }
            },
            {
                '$group': {
                    '_id': {
                        'owner': parent_org
                    },
                    'open_tix_opened_less_than_7_days_ago': {
                        '$sum': {
                            '$cond': [
                                {
                                    '$gte': [
                                        '$time_opened',
                                        current_date - timedelta(days=7)
                                    ]
                                },
                                1,
                                0
                            ]
                        }
                    },
                    'open_tix_opened_7-15_days_ago': {
                        '$sum': {
                            '$cond': [
                                {
                                    '$and': [
                                        {
                                            '$lt': [
                                                '$time_opened',
                                                current_date - timedelta(days=7)
                                            ]
                                        },
                                        {
                                            '$gte': [
                                                '$time_opened',
                                                current_date - timedelta(days=15)
                                            ]
                                        }
                                    ]
                                },
                                1,
                                0
                            ]
                        }
                    },
                    'open_tix_opened_15-30_days_ago': {
                        '$sum': {
                            '$cond': [
                                {
                                    '$and': [
                                        {
                                            '$lt': [
                                                '$time_opened',
                                                current_date - timedelta(days=15)
                                            ]
                                        },
                                        {
                                            '$gte': [
                                                '$time_opened',
                                                current_date - timedelta(days=30)
                                            ]
                                        }
                                    ]
                                },
                                1,
                                0
                            ]
                        }
                    },
                    'open_tix_opened_30-90_days_ago': {
                        '$sum': {
                            '$cond': [
                                {
                                    '$and': [
                                        {
                                            '$lt': [
                                                '$time_opened',
                                                current_date - timedelta(days=30)
                                            ]
                                        },
                                        {
                                            '$gte': [
                                                '$time_opened',
                                                current_date - timedelta(days=90)
                                            ]
                                        }
                                    ]
                                },
                                1,
                                0
                            ]
                        }
                    },
                    'open_tix_opened_more_than_90_days_ago': {
                        '$sum': {
                            '$cond': [
                                {
                                    '$lt': [
                                        '$time_opened',
                                        current_date - timedelta(days=90)
                                    ]
                                },
                                1,
                                0
                            ]
                        }
                    },
                    'open_overdue_tix': {
                        '$sum': {
                            '$cond': [
                                {
                                    '$lt': [
                                        '$time_opened',
                                        current_date - timedelta(days=days_until_tix_overdue)
                                    ]
                                },
                                1,
                                0
                            ]
                        }
                    },
                    'open_tix_count': {'$sum': 1}
                }
            }
        ], database.TICKET_COLLECTION

    def __active_hosts_pl(self):
        return [
            {
                '$match': {
                    'state.up': True
                }
            },
            {
                '$group': {
                    '_id': {'owner': '$owner'},
                    'active_hosts_count': {'$sum': 1}
                }
            }
        ], database.HOST_COLLECTION

    def __active_hosts_for_orgs_pl(self, parent_org, descendant_orgs):
        return [
            {
                '$match': {
                    'state.up': True,
                    'owner': {'$in': [parent_org] + descendant_orgs}
                }
            },
            {
                '$group': {
                    '_id': {'owner': parent_org},
                    'active_hosts_count': {'$sum': 1}
                }
            }
        ], database.HOST_COLLECTION

    def __load_ticket_age_data(self, start_date, severity,
                               graph_bucket_cutoff_days, org_list):
        tomorrow = self.__generated_time + timedelta(days=1)
        days_to_graph = pd.to_datetime(pd.date_range(start_date,
                                                     self.__generated_time),
                                       utc=True)

        # Calculate Buckets
        tix = self.__cyhy_db.TicketDoc.find(
            {
                'details.severity': severity,
                'false_positive': False,
                'owner': {'$in': org_list},
                '$or': [
                    {
                        'time_closed': {'$gte': start_date}
                    },
                    {
                        'time_closed': None
                    }
                ]
            },
            {
                '_id': False,
                'time_opened': True,
                'time_closed': True,
                'source': 'nessus'
            }
        )
        tix = list(tix)
        if len(tix):
            df = DataFrame(tix)
            # for accounting purposes, say all open tix will close tomorrow
            df.time_closed = df.time_closed.fillna(tomorrow, downcast='infer')
            # convert times to datetime64
            df.time_closed = pd.to_datetime(df.time_closed, utc=True)
            df.time_opened = pd.to_datetime(df.time_opened, utc=True)

            results_df = DataFrame(index=days_to_graph,
                                   columns=['young', 'old', 'total'])
            old_delta = np.timedelta64(graph_bucket_cutoff_days, 'D')

            for start_of_day, values in results_df.iterrows():
                end_of_day = start_of_day + \
                             np.timedelta64(1, 'D') - \
                             np.timedelta64(1, 'ns')
                open_on_day_mask = (df.time_opened <= end_of_day) & \
                                   (df.time_closed > start_of_day)
                age_on_date = start_of_day - df.time_opened
                age_on_date_masked = age_on_date.mask(
                                        open_on_day_mask is False)
                values['total'] = open_on_day_mask.value_counts().get(True, 0)
                values['young'] = (age_on_date_masked <
                                   old_delta).value_counts().get(True, 0)
                values['old'] = (age_on_date_masked >=
                                 old_delta).value_counts().get(True, 0)
            return results_df
        else:
            return DataFrame([])

    def __load_open_tickets(self, severity, org_list):
        return list(self.__cyhy_db.tickets.find(
            {
                'open': True,
                'owner': {
                    '$in': self.__all_cybex_orgs_with_descendants
                },
                'details.severity': severity,
                'false_positive': False
            },
            {
                'time_opened': 1
            }
        ))

    def __run_vuln_scan_queries(self, cybex_orgs):
        # If an org has descendants, we only want the top-level org to show up
        # in the Scorecard.  Make list of orgs that have children and their
        # request docs so their child data can be accumulated later.
        orgs_with_descendants = []
        requests_with_descendants = []
        self.__results['vuln-scan'] = {'addresses': []}
        for r in self.__requests:
            if r.get('children'):
                orgs_with_descendants.append(r['_id'])
                requests_with_descendants.append(r)
            else:
                # Grab the number of addresses for every org that has no
                # descendants.  For consistency and ease of processing later,
                # store number of addresses in same format as pipeline
                # queries below.
                self.__results['vuln-scan']['addresses'].append(
                    {'_id': {'owner': r['_id']},
                     'addresses_count': len(r.networks)})

        # Get relevant critical-severity ticket data
        pipeline_collection = self.__open_tix_opened_in_date_range_pl(
                                CRITICAL_SEVERITY, self.__generated_time,
                                DAYS_UNTIL_OVERDUE_CRITICAL)
        self.__results['vuln-scan']['open_critical_ticket_counts'] = \
            database.run_pipeline_cursor(pipeline_collection, self.__cyhy_db)

        # Get relevant high-severity ticket data
        pipeline_collection = self.__open_tix_opened_in_date_range_pl(
                                HIGH_SEVERITY, self.__generated_time,
                                DAYS_UNTIL_OVERDUE_HIGH)
        self.__results['vuln-scan']['open_high_ticket_counts'] = \
            database.run_pipeline_cursor(pipeline_collection, self.__cyhy_db)

        pipeline_collection = self.__active_hosts_pl()
        self.__results['vuln-scan']['active_hosts'] = \
            database.run_pipeline_cursor(pipeline_collection, self.__cyhy_db)

        # Throw out data from orgs with descendants
        # list(self.__results[results_field]) iterates over a *copy* of the
        # list so items can be properly removed from the original
        for results_field in ['open_critical_ticket_counts',
                              'open_high_ticket_counts',
                              'active_hosts']:
            for r in list(self.__results['vuln-scan'][results_field]):
                if r['_id']['owner'] in orgs_with_descendants:
                    self.__results['vuln-scan'][results_field].remove(r)

        # Pull grouped data for orgs with descendants and add it to results
        for r in requests_with_descendants:
            descendants = self.__cyhy_db.RequestDoc.get_all_descendants(
                r['_id'])

            pipeline_collection = \
                self.__open_tix_opened_in_date_range_for_orgs_pl(
                    CRITICAL_SEVERITY, self.__generated_time, r['_id'],
                    descendants, DAYS_UNTIL_OVERDUE_CRITICAL)
            self.__results['vuln-scan']['open_critical_ticket_counts'] += \
                database.run_pipeline_cursor(pipeline_collection,
                                             self.__cyhy_db)

            pipeline_collection = \
                self.__open_tix_opened_in_date_range_for_orgs_pl(
                    HIGH_SEVERITY, self.__generated_time, r['_id'],
                    descendants, DAYS_UNTIL_OVERDUE_HIGH)
            self.__results['vuln-scan']['open_high_ticket_counts'] += \
                database.run_pipeline_cursor(pipeline_collection,
                                             self.__cyhy_db)

            pipeline_collection = self.__active_hosts_for_orgs_pl(
                r['_id'], descendants)
            self.__results['vuln-scan']['active_hosts'] += \
                database.run_pipeline_cursor(pipeline_collection,
                                             self.__cyhy_db)

            # Top-level org count of addresses (networks)
            address_count = len(r.networks)
            # Iterate through descendants and grab count of addresses
            for descendant_id in descendants:
                address_count += len(self.__cyhy_db.RequestDoc.get_by_owner(
                    descendant_id).networks)
            self.__results['vuln-scan']['addresses'].append(
                {'_id': {'owner': r['_id']},
                 'addresses_count': address_count})

    def __run_trustymail_queries(self, cybex_orgs):
        # Latest Trustymail metrics for base domains
        self.__results['latest_cybex_trustymail_base_domains'] = [
            i['domain']
            for i in self.__scan_db.trustymail.find(
                    {
                        'latest': True,
                        'is_base_domain': True,
                        'agency.id': {
                            '$in': cybex_orgs
                        }
                    },
                    {
                        '_id': 0,
                        'domain': 1
                    }
            )
        ]

        self.__results['trustymail_base_domains'] = list(
            self.__scan_db.trustymail.aggregate(
                [
                    {
                        '$match': {
                            'latest': True,
                            'domain': {
                                '$in': self.__results[
                                    'latest_cybex_trustymail_base_domains'
                                ]
                            }
                        }
                    },
                    # Pull in data from sslyze_scan collection so weak
                    # crypto status can be determined
                    {
                        '$lookup': {
                            'from': 'sslyze_scan',
                            'localField': 'domain',
                            'foreignField': 'domain',
                            'as': 'sslyze_data'
                        }
                    },
                    {
                        '$project': {
                            'agency.id': '$agency.id',
                            'scan_date': '$scan_date',
                            'live': '$live',
                            'valid_spf': '$valid_spf',
                            'valid_dmarc': '$valid_dmarc',
                            'dmarc_policy': '$dmarc_policy',
                            'dmarc_subdomain_policy': '$dmarc_subdomain_policy',
                            'dmarc_policy_percentage': '$dmarc_policy_percentage',
                            'has_bod1801_dmarc_rua_uri': {
                                '$cond': [
                                    {
                                        '$eq': [
                                            {
                                                '$filter': {
                                                    'input': '$aggregate_report_uris',
                                                    'as': 'agg_report_uri',
                                                    'cond': {
                                                        '$eq': [
                                                            '$$agg_report_uri.uri',
                                                            BOD1801_DMARC_RUA_URI
                                                        ]
                                                    }
                                                }
                                            },
                                            []
                                        ]
                                    },
                                    False,
                                    True
                                ]
                            },
                            'domain_supports_smtp': '$domain_supports_smtp',
                            'domain_supports_starttls': '$domain_supports_starttls',
                            'is_missing_starttls':
                            {
                                '$and': [
                                    {'$eq': ['$domain_supports_smtp', True]},
                                    {'$eq': ['$domain_supports_starttls', False]}
                                ]
                            },
                            # has_weak_mail_crypto projection can be
                            # simplified by changing $lookup above to
                            # use an uncorrelated subquery (Mongo 3.6
                            # or later)
                            'has_weak_mail_crypto':
                            {
                                '$cond': [
                                    {
                                        '$eq': [
                                            {
                                                '$filter': {
                                                    'input': '$sslyze_data',
                                                    'as': 'sslyze',
                                                    'cond': {
                                                        '$and': [
                                                            {'$eq': ['$$sslyze.latest', True]},
                                                            {
                                                                '$or': [
                                                                    {
                                                                        '$eq': [
                                                                            '$$sslyze.scanned_port',
                                                                            25
                                                                        ]
                                                                    },
                                                                    {
                                                                        '$eq': [
                                                                            '$$sslyze.scanned_port',
                                                                            587
                                                                        ]
                                                                    },
                                                                    {
                                                                        '$eq': [
                                                                            '$$sslyze.scanned_port',
                                                                            465
                                                                        ]
                                                                    }
                                                                ]
                                                            },
                                                            {
                                                                '$or': [
                                                                    {
                                                                        '$eq': [
                                                                            '$$sslyze.sslv2',
                                                                            True
                                                                        ]
                                                                    },
                                                                    {
                                                                        '$eq': [
                                                                            '$$sslyze.sslv3',
                                                                            True
                                                                        ]
                                                                    },
                                                                    {
                                                                        '$eq': [
                                                                            '$$sslyze.any_3des',
                                                                            True
                                                                        ]
                                                                    },
                                                                    {
                                                                        '$eq': [
                                                                            '$$sslyze.any_rc4',
                                                                            True
                                                                        ]
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    }
                                                }
                                            },
                                            []
                                        ]
                                    },
                                    False,
                                    True
                                ]
                            }
                        }
                    },
                    {
                        '$group' : {
                            '_id': '$agency.id',
                            'earliest_scan_date': {'$min': '$scan_date'},
                            'domain_count': {'$sum': 1},
                            'live_domain_count': {
                                '$sum': {
                                    '$cond': [
                                        {
                                            '$eq': ['$live', True]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },
                            'live_valid_dmarc_count': {
                                '$sum': {
                                    '$cond': [
                                        {
                                            '$and': [
                                                {'$eq': ['$live', True]},
                                                {'$eq': ['$valid_dmarc', True]}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },
                            'live_dmarc_reject_count': {
                                '$sum': {
                                    '$cond': [
                                        {
                                            '$and': [
                                                {'$eq': ['$live', True]},
                                                {'$eq': ['$valid_dmarc', True]},
                                                {'$eq': ['$dmarc_policy', 'reject']},
                                                {
                                                  '$or': [
                                                      {'$eq': ['$is_base_domain', False]},
                                                      {'$eq': ['$dmarc_subdomain_policy', 'reject']},
                                                  ]
                                                },
                                                {'$eq': ['$dmarc_policy_percentage', 100]}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },
                            'live_has_bod1801_dmarc_uri_count': {
                                '$sum': {
                                    '$cond': [
                                        {
                                            '$and': [
                                                {'$eq': ['$live', True]},
                                                {'$eq': ['$valid_dmarc', True]},
                                                {'$eq': ['$has_bod1801_dmarc_rua_uri', True]}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },
                            'live_valid_spf_count': {
                                '$sum': {
                                    '$cond': [
                                        {
                                            '$and': [
                                                {'$eq': ['$live', True]},
                                                {'$eq': ['$valid_spf', True]}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },
                            'live_missing_starttls_count': {
                                '$sum': {
                                    '$cond': [
                                        {
                                            '$and': [
                                                {'$eq': ['$live', True]},
                                                {'$eq': ['$is_missing_starttls', True]}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },
                            'live_no_weak_crypto_count': {
                                '$sum': {
                                    '$cond': [
                                        {
                                            '$and': [
                                                {'$eq': ['$live', True]},
                                                {'$eq': ['$has_weak_mail_crypto', False]}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },
                            'live_bod1801_dmarc_compliant_count': {
                                '$sum': {
                                    '$cond': [
                                        {
                                            '$and': [
                                                {'$eq': ['$live', True]},
                                                {'$eq': ['$valid_dmarc', True]},
                                                {'$eq': ['$dmarc_policy', 'reject']},
                                                {
                                                  '$or': [
                                                      {'$eq': ['$is_base_domain', False]},
                                                      {'$eq': ['$dmarc_subdomain_policy', 'reject']},
                                                  ]
                                                },
                                                {'$eq': ['$dmarc_policy_percentage', 100]},
                                                {'$eq': ['$has_bod1801_dmarc_rua_uri', True]}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },
                            'live_bod1801_email_compliant_count': {
                                '$sum': {
                                    '$cond': [
                                        {
                                            '$and': [
                                                {'$eq': ['$live', True]},
                                                {'$eq': ['$valid_dmarc', True]},
                                                {'$eq': ['$dmarc_policy', 'reject']},
                                                {
                                                  '$or': [
                                                      {'$eq': ['$is_base_domain', False]},
                                                      {'$eq': ['$dmarc_subdomain_policy', 'reject']},
                                                  ]
                                                },
                                                {'$eq': ['$dmarc_policy_percentage', 100]},
                                                {'$eq': ['$has_bod1801_dmarc_rua_uri', True]},
                                                {'$eq': ['$is_missing_starttls', False]},
                                                {'$eq': ['$valid_spf', True]},
                                                {'$eq': ['$has_weak_mail_crypto', False]}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            }
                        }
                    },
                    {
                        '$project': {
                            '_id': 1,
                            'earliest_scan_date': 1,
                            'domain_count': 1,
                            'live_domain_count': 1,
                            'live_valid_dmarc_count': 1,
                            'live_dmarc_reject_count': 1,
                            'live_has_bod1801_dmarc_uri_count': 1,
                            'live_valid_spf_count': 1,
                            # For base domains, live_spf_covered_count
                            # is the same thing as
                            # live_valid_spf_count. It is included
                            # here for consistency in processing the
                            # query results.
                            'live_spf_covered_count': '$live_valid_spf_count',
                            'live_missing_starttls_count': 1,
                            'live_no_weak_crypto_count': 1,
                            'live_bod1801_dmarc_compliant_count': 1,
                            'live_bod1801_email_compliant_count': 1,
                            'live_supports_starttls_count': {
                                '$subtract': [
                                    '$live_domain_count',
                                    '$live_missing_starttls_count'
                                ]
                            },
                            'live_bod1801_dmarc_non_compliant_count': {
                                '$subtract': [
                                    '$live_domain_count',
                                    '$live_bod1801_dmarc_compliant_count'
                                ]
                            },
                            'live_bod1801_email_non_compliant_count': {
                                '$subtract': [
                                    '$live_domain_count',
                                    '$live_bod1801_email_compliant_count'
                                ]
                            }
                        }
                    },
                    {
                        '$sort': {'_id': 1}
                    }
                ], cursor={}
            )
        )

        # Latest Trustymail metrics for base domains and subdomains that
        # support SMTP
        self.__results[
            'latest_cybex_trustymail_base_domains_and_smtp_subdomains'
        ] = [
            i['domain']
            for i in self.__scan_db.trustymail.find(
                    {
                        'latest': True,
                        'agency.id': {'$in': cybex_orgs},
                        '$or': [
                            {'is_base_domain': True},
                            {'domain_supports_smtp': True}
                        ]
                    },
                    {
                        '_id': 0,
                        'domain': 1
                    }
            )
        ]

        self.__results['trustymail_base_domains_and_smtp_subdomains'] = list(
            self.__scan_db.trustymail.aggregate(
                [
                    {
                        '$match': {
                            'latest': True,
                            'domain': {
                                '$in': self.__results['latest_cybex_trustymail_base_domains_and_smtp_subdomains']
                            }
                        }
                    },
                    # Pull in data from sslyze_scan collection so weak
                    # crypto status can be determined
                    {
                        '$lookup': {
                            'from': 'sslyze_scan',
                            'localField': 'domain',
                            'foreignField': 'domain',
                            'as': 'sslyze_data'
                        }
                    },
                    {
                        '$project': {
                            'agency.id': '$agency.id',
                            'live': '$live',
                            'is_base_domain': '$is_base_domain',
                            'spf_record': '$spf_record',
                            'valid_spf': '$valid_spf',
                            'valid_dmarc': '$valid_dmarc',
                            'valid_dmarc_base_domain': '$valid_dmarc_base_domain',
                            'dmarc_policy': '$dmarc_policy',
                            'dmarc_subdomain_policy': '$dmarc_subdomain_policy',
                            'dmarc_policy_percentage': '$dmarc_policy_percentage',
                            'has_bod1801_dmarc_rua_uri': {
                                '$cond': [
                                    {
                                        '$eq': [
                                            {
                                                '$filter': {
                                                    'input': '$aggregate_report_uris',
                                                    'as': 'agg_report_uri',
                                                    'cond': {
                                                        '$eq': [
                                                            '$$agg_report_uri.uri',
                                                            BOD1801_DMARC_RUA_URI
                                                        ]
                                                    }
                                                }
                                            },
                                            []
                                        ]
                                    },
                                    False,
                                    True
                                ]
                            },
                            'domain_supports_smtp':' $domain_supports_smtp',
                            'domain_supports_starttls': '$domain_supports_starttls',
                            'is_missing_starttls': {
                                '$and': [
                                    {'$eq': ['$domain_supports_smtp', True]},
                                    {'$eq': ['$domain_supports_starttls', False]}
                                ]
                            },
                            # has_weak_mail_crypto projection can be
                            # simplified by changing $lookup above to
                            # use an uncorrelated subquery (Mongo 3.6
                            # or later)
                            'has_weak_mail_crypto': {
                                '$cond': [
                                    {
                                        '$eq': [
                                            {
                                                '$filter': {
                                                    'input': '$sslyze_data',
                                                    'as': 'sslyze',
                                                    'cond': {
                                                        '$and': [
                                                            {'$eq': ['$$sslyze.latest', True]},
                                                            {
                                                                '$or': [
                                                                    {
                                                                        '$eq': [
                                                                            '$$sslyze.scanned_port',
                                                                            25
                                                                        ]
                                                                    },
                                                                    {
                                                                        '$eq': [
                                                                            '$$sslyze.scanned_port',
                                                                            587
                                                                        ]
                                                                    },
                                                                    {
                                                                        '$eq': [
                                                                            '$$sslyze.scanned_port',
                                                                            465
                                                                        ]
                                                                    }
                                                                ]
                                                            },
                                                            {
                                                                '$or': [
                                                                    {
                                                                        '$eq': [
                                                                            '$$sslyze.sslv2',
                                                                            True
                                                                        ]
                                                                    },
                                                                    {
                                                                        '$eq': [
                                                                            '$$sslyze.sslv3',
                                                                            True
                                                                        ]
                                                                    },
                                                                    {
                                                                        '$eq': [
                                                                            '$$sslyze.any_3des',
                                                                            True
                                                                        ]
                                                                    },
                                                                    {
                                                                        '$eq': [
                                                                            '$$sslyze.any_rc4',
                                                                            True
                                                                        ]
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    }
                                                }
                                            },
                                            []
                                        ]
                                    },
                                    False,
                                    True
                                ]
                            }
                        }
                    },
                    {
                        '$group': {
                            '_id': '$agency.id',
                            'domain_count': {'$sum': 1},
                            'live_domain_count': {
                                '$sum': {
                                    '$cond': [
                                        {
                                            '$eq': ['$live', True]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },
                            # live_valid_dmarc_count here means either
                            # you have your own valid DMARC record or
                            # your base domain does
                            'live_valid_dmarc_count': {
                                '$sum': {
                                    '$cond': [
                                        {
                                            '$and': [
                                                {'$eq': ['$live', True]},
                                                {
                                                    '$or': [
                                                        {
                                                            '$eq': [
                                                                '$valid_dmarc',
                                                                True
                                                            ]
                                                        },
                                                        {
                                                            '$eq': [
                                                                '$valid_dmarc_base_domain',
                                                                True
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },
                            # once again, either you or your base
                            # domain have to have valid DMARC to get
                            # credit
                            'live_dmarc_reject_count': {
                                '$sum': {
                                    '$cond': [
                                        {
                                            '$and': [
                                                {'$eq': ['$live', True]},
                                                {
                                                    '$or': [
                                                        {
                                                            '$eq': [
                                                                '$valid_dmarc',
                                                                True
                                                            ]
                                                        },
                                                        {
                                                            '$eq': [
                                                                '$valid_dmarc_base_domain',
                                                                True
                                                            ]
                                                        }
                                                    ]
                                                },
                                                {'$eq': ['$dmarc_policy', 'reject']},
                                                {
                                                  '$or': [
                                                      {'$eq': ['$is_base_domain', False]},
                                                      {'$eq': ['$dmarc_subdomain_policy', 'reject']},
                                                  ]
                                                },
                                                {'$eq': ['$dmarc_policy_percentage', 100]}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },
                            # once again, either you or your base
                            # domain have to have valid DMARC to get
                            # credit
                            'live_has_bod1801_dmarc_uri_count': {
                                '$sum': {
                                    '$cond': [
                                        {
                                            '$and': [
                                                {'$eq': ['$live', True]},
                                                {
                                                    '$or': [
                                                        {
                                                            '$eq': [
                                                                '$valid_dmarc',
                                                                True
                                                            ]
                                                        },
                                                        {
                                                            '$eq': [
                                                                '$valid_dmarc_base_domain',
                                                                True
                                                            ]
                                                        }
                                                    ]
                                                },
                                                {'$eq': ['$has_bod1801_dmarc_rua_uri', True]}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },
                            # Even though the BOD says SPF is required
                            # for base (2nd-level) domains, we are
                            # also measuring it for mail-sending hosts
                            # (in live_valid_spf_count); more info
                            # about this in CYHY-592
                            'live_valid_spf_count': {
                                '$sum': {
                                    '$cond': [
                                        {
                                            '$and': [
                                                {'$eq': ['$live', True]},
                                                {'$eq': ['$valid_spf', True]}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },
                            # live_spf_covered_count was added with
                            # CYHY-754 to give credit for non-base
                            # domains without SPF records that are
                            # "covered" by a DMARC policy of reject
                            'live_spf_covered_count': {
                                '$sum': {
                                    '$cond': [
                                        {
                                            '$or': [
                                                {
                                                    '$and': [
                                                        {'$eq': ['$live', True]},
                                                        {'$eq': ['$valid_spf', True]}
                                                    ]
                                                },
                                                {
                                                    '$and': [
                                                        {'$eq': ['$is_base_domain', False]},
                                                        {'$eq': ['$live', True]},
                                                        {'$eq': ['$spf_record', False]},
                                                        {
                                                            '$or': [
                                                                {'$eq': ['$valid_dmarc', True]},
                                                                {'$eq': ['$valid_dmarc_base_domain', True]}
                                                            ]
                                                        },
                                                        {'$eq': ['$dmarc_policy', 'reject']},
                                                        {
                                                            '$or': [
                                                                {'$eq': ['$is_base_domain', False]},
                                                                {'$eq': ['$dmarc_subdomain_policy', 'reject']},
                                                            ]
                                                        },
                                                        {'$eq': ['$dmarc_policy_percentage', 100]}
                                                    ]
                                                }
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },
                            'live_missing_starttls_count': {
                                '$sum': {
                                    '$cond': [
                                        {
                                            '$and': [
                                                {'$eq': ['$live', True]},
                                                {'$eq': ['$is_missing_starttls', True]}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },
                            'live_no_weak_crypto_count': {
                                '$sum': {
                                    '$cond': [
                                        {
                                            '$and': [
                                                {'$eq': ['$live', True]},
                                                {'$eq': ['$has_weak_mail_crypto', False]}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },
                            'live_bod1801_dmarc_compliant_count': {
                                '$sum': {
                                    '$cond': [
                                        {
                                            '$and': [
                                                {'$eq': ['$live', True]},
                                                {
                                                    '$or': [
                                                        {'$eq': ['$valid_dmarc', True]},
                                                        {'$eq': ['$valid_dmarc_base_domain', True]}
                                                    ]
                                                },
                                                {'$eq': ['$dmarc_policy', 'reject']},
                                                {
                                                    '$or': [
                                                        {'$eq': ['$is_base_domain', False]},
                                                        {'$eq': ['$dmarc_subdomain_policy', 'reject']},
                                                    ]
                                                },
                                                {'$eq': ['$dmarc_policy_percentage', 100]},
                                                {'$eq': ['$has_bod1801_dmarc_rua_uri', True]}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            },
                            'live_bod1801_email_compliant_count': {
                                '$sum': {
                                    '$cond': [
                                        {
                                            '$and': [
                                                {'$eq': ['$live', True]},
                                                {
                                                    '$or': [
                                                        {'$eq': ['$valid_dmarc', True]},
                                                        {'$eq': ['$valid_dmarc_base_domain', True]}
                                                    ]
                                                },
                                                {'$eq': ['$dmarc_policy', 'reject']},
                                                {
                                                    '$or': [
                                                        {'$eq': ['$is_base_domain', False]},
                                                        {'$eq': ['$dmarc_subdomain_policy', 'reject']},
                                                    ]
                                                },
                                                {'$eq': ['$dmarc_policy_percentage', 100]},
                                                {'$eq': ['$has_bod1801_dmarc_rua_uri', True]},
                                                {'$eq': ['$is_missing_starttls', False]},
                                                {
                                                    '$or': [
                                                        {'$eq': ['$valid_spf', True]},
                                                        {
                                                            '$and': [
                                                                {'$eq': ['$is_base_domain', False]},
                                                                {'$eq': ['$spf_record', False]},
                                                                {
                                                                    '$or': [
                                                                        {'$eq': ['$valid_dmarc', True]},
                                                                        {'$eq': ['$valid_dmarc_base_domain', True]}
                                                                    ]
                                                                },
                                                                {'$eq': ['$dmarc_policy', 'reject']},
                                                                {
                                                                    '$or': [
                                                                        {'$eq': ['$is_base_domain', False]},
                                                                        {'$eq': ['$dmarc_subdomain_policy', 'reject']},
                                                                    ]
                                                                },
                                                                {'$eq': ['$dmarc_policy_percentage', 100]}
                                                            ]
                                                        }
                                                    ]
                                                },
                                                {'$eq': ['$has_weak_mail_crypto', False]}
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            }
                        }
                    },
                    {
                        '$project': {
                            '_id': 1,
                            'domain_count': 1,
                            'live_domain_count': 1,
                            'live_valid_dmarc_count': 1,
                            'live_dmarc_reject_count': 1,
                            'live_has_bod1801_dmarc_uri_count': 1,
                            'live_valid_spf_count': 1,
                            'live_spf_covered_count': 1,
                            'live_missing_starttls_count': 1,
                            'live_no_weak_crypto_count': 1,
                            'live_bod1801_dmarc_compliant_count': 1,
                            'live_bod1801_email_compliant_count': 1,
                            'live_supports_starttls_count': {
                                '$subtract': [
                                    '$live_domain_count',
                                    '$live_missing_starttls_count'
                                ]
                            },
                            'live_bod1801_dmarc_non_compliant_count': {
                                '$subtract': [
                                    '$live_domain_count',
                                    '$live_bod1801_dmarc_compliant_count'
                                ]
                            },
                            'live_bod1801_email_non_compliant_count': {
                                '$subtract': [
                                    '$live_domain_count',
                                    '$live_bod1801_email_compliant_count'
                                ]
                            }
                        }
                    },
                    {
                        '$sort': {'_id': 1}
                    }
                ],
                cursor={}
            )
        )

        # Trustymail DMARC summary Metrics (live base domains only)
        self.__results['trustymail_dmarc_summary'] = list(
            self.__scan_db.trustymail.aggregate([
                {
                    '$match': {
                        'agency.id': {'$in': cybex_orgs},
                        'live': True,
                        'is_base_domain': True
                    }
                },
                {
                    '$project': {
                        'domain': '$domain',
                        'scan_date': '$scan_date',
                        'dmarc_policy': '$dmarc_policy',
                        # Since dmarc_subdomain_policy was added to
                        # our data recently
                        # (https://github.com/cisagov/saver/pull/39)
                        # and we don't have historical data, we
                        # default its value to 'reject' when it
                        # doesn't exist in the data. This gives
                        # results closest to what we had prior to
                        # adding dmarc_subdomain_policy.
                        #
                        # The $ifNull expression below can be removed
                        # in 9 weeks when we have enough historical
                        # data containing this field.
                        'dmarc_subdomain_policy': {
                            '$ifNull': [
                                '$dmarc_subdomain_policy', 'reject'
                            ]
                        },
                        'dmarc_policy_percentage': '$dmarc_policy_percentage',
                        'valid_dmarc': '$valid_dmarc',
                        'dmarc_record': '$dmarc_record',
                        # Filter aggregate_report_uris to search for
                        # BOD1801_DMARC_RUA_URI in the uri field, then
                        # check if the resulting array is empty
                        # (has_bod1801_dmarc_rua_uri == False), else
                        # it's True
                        'has_bod1801_dmarc_rua_uri': {
                            '$cond': [
                                {
                                    '$eq': [
                                        {
                                            '$filter': {
                                                'input': '$aggregate_report_uris',
                                                'as': 'agg_report_uri',
                                                'cond': {
                                                    '$eq': [
                                                        '$$agg_report_uri.uri',
                                                        BOD1801_DMARC_RUA_URI
                                                    ]
                                                }
                                            }
                                        },
                                        []
                                    ]
                                },
                                False,
                                True
                            ]
                        },
                    }
                },
                {
                    '$group': {
                        '_id': '$scan_date',
                        'base_domain_count': {'$sum': 1},
                        'dmarc_policy_none': {
                            '$sum': {
                                '$cond': [
                                    {
                                        '$and': [
                                            {'$eq': ['$dmarc_policy', 'none']},
                                            {'$eq': ['$valid_dmarc', True]}
                                        ]
                                    },
                                    1,
                                    0
                                ]
                            }
                        },
                        'dmarc_policy_quarantine': {
                            '$sum': {
                                '$cond': [
                                    {
                                        '$and': [
                                            {'$eq': ['$dmarc_policy', 'quarantine']},
                                            {'$eq': ['$valid_dmarc', True]}
                                        ]
                                    },
                                    1,
                                    0
                                ]
                            }
                        },
                        'dmarc_policy_reject': {
                            '$sum': {
                                '$cond': [
                                    {
                                        '$and': [
                                            {'$eq': ['$dmarc_policy', 'reject']},
                                            {'$eq': ['$valid_dmarc', True]},
                                            {
                                                '$or': [
                                                    {'$eq': ['$is_base_domain', False]},
                                                    {'$eq': ['$dmarc_subdomain_policy', 'reject']},
                                                ]
                                            },
                                            {'$eq': ['$dmarc_policy_percentage', 100]}
                                        ]
                                    },
                                    1,
                                    0
                                ]
                            }
                        },
                        'dmarc_correct_rua': {
                            '$sum': {
                                '$cond': [
                                    {
                                        '$and': [
                                            {'$eq': ['$has_bod1801_dmarc_rua_uri', True]},
                                            {'$eq': ['$valid_dmarc', True]}
                                        ]
                                    },
                                    1,
                                    0
                                ]
                            }
                        },
                        'invalid_dmarc_record': {
                            '$sum': {
                                '$cond': [
                                    {
                                        '$and': [
                                            {'$eq': ['$dmarc_record', True]},
                                            {'$eq': ['$valid_dmarc', False]}
                                        ]
                                    },
                                    1,
                                    0
                                ]
                            }
                        },
                        'no_dmarc_record': {
                            '$sum': {
                                '$cond': [
                                    {'$eq': ['$dmarc_record', False]},
                                    1,
                                    0
                                ]
                            }
                        }
                    }
                },
                # Reverse sort + limit = most-recent n results
                {
                    '$sort': {'_id': -1}
                },
                {
                    '$limit': TRUSTYMAIL_SUMMARY_SCAN_DATE_COUNT
                }
            ], cursor={})
        )

    def __run_https_scan_queries(self, cybex_orgs):
        # https-scan queries:
        # Drop domains that are OCSP sites, since they are to be excluded
        self.__results['latest_cybex_https_scan_live_hostnames'] = [
            i['domain']
            for i in self.__scan_db.https_scan.find({
                    'latest': True,
                    'live': True,
                    'agency.id': {
                        '$in': cybex_orgs
                    },
                    # I get an error in Python 3 if I just use
                    # self.__ocsp_exclusions.keys() here.  This is
                    # because in Python 3 dict.keys() returns a view,
                    # not an actual list.
                    #
                    # Since we're moving to Python 3 eventually, it
                    # seems reasonable to leave the explicit list(...)
                    # in.
                    #
                    # TODO: Update this comment after moving to Python
                    # 3.
                    'domain': {'$nin': list(self.__ocsp_exclusions.keys())}
            }, {
                '_id': 0,
                'domain': 1
            })
        ]

        self.__results['https-scan'] = list(
            self.__scan_db.https_scan.aggregate([
                {
                    '$match': {
                        'latest': True,
                        'domain': {
                            '$in': self.__results['latest_cybex_https_scan_live_hostnames']
                        }
                    }
                },
                # Pull in data from sslyze_scan collection so weak
                # crypto status can be determined
                {
                    '$lookup': {
                        'from': 'sslyze_scan',
                        'localField': 'domain',
                        'foreignField': 'domain',
                        'as':'sslyze_data'
                    }
                },
                {
                    '$project': {
                        'agency.id': '$agency.id',
                        'scan_date': '$scan_date',
                        # If hsts_base_domain_preloaded is True, a
                        # domain automatically gets credit for
                        # supporting HTTPS, enforcing HTTPS, and
                        # having strong HSTS Even if pshtt says that
                        # the domain does not support HTTPS, enforce
                        # HTTPS, or have strong HSTS
                        'domain_supports_https': {
                            '$cond': [
                                {
                                    '$or': [
                                        {
                                            '$eq': ['$domain_supports_https', True]
                                        },
                                        {
                                            '$eq': ['$hsts_base_domain_preloaded', True]
                                        }
                                    ]
                                },
                                True,
                                False
                            ]
                        },
                        'domain_enforces_https': {
                            '$cond': [
                                {
                                    '$or': [
                                        {
                                            '$eq': ['$domain_enforces_https', True]
                                        },
                                        {
                                            '$eq': ['$hsts_base_domain_preloaded', True]
                                        }
                                    ]
                                },
                                True,
                                False
                            ]
                        },
                        'domain_uses_strong_hsts': {
                            '$cond': [
                                {
                                    '$or': [
                                        {
                                            '$eq': ['$domain_uses_strong_hsts', True]
                                        },
                                        {
                                            '$eq': ['$hsts_base_domain_preloaded', True]
                                        },
                                        {
                                            '$and': [
                                                {
                                                    '$eq': ['$https_full_connection', False]
                                                },
                                                {
                                                    '$eq': ['$https_client_auth_required', True]
                                                }
                                            ]
                                        }
                                    ]
                                },
                                True,
                                False
                            ]
                        },
                        # has_weak_web_crypto projection can be
                        # simplified by changing $lookup above to use
                        # an uncorrelated subquery (Mongo 3.6 or
                        # later)
                        'has_weak_web_crypto': {
                            '$cond': [
                                {
                                    '$eq': [
                                        {
                                            '$filter': {
                                                'input': '$sslyze_data',
                                                'as':'sslyze',
                                                'cond': {
                                                    '$and': [
                                                        {
                                                            '$eq': ['$$sslyze.latest', True]
                                                        },
                                                        {
                                                            '$eq': ['$$sslyze.scanned_port', 443]
                                                        },
                                                        {
                                                            '$or': [
                                                                {
                                                                    '$eq': ['$$sslyze.sslv2', True]
                                                                },
                                                                {
                                                                    '$eq': ['$$sslyze.sslv3', True]
                                                                },
                                                                {
                                                                    '$eq': ['$$sslyze.any_3des', True]
                                                                },
                                                                {
                                                                    '$eq': ['$$sslyze.any_rc4', True]
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            }
                                        },
                                        []
                                    ]
                                },
                                False,
                                True
                            ]
                        }
                    }
                },
                {
                    '$group': {
                        '_id': '$agency.id',
                        'earliest_scan_date': {
                            '$min': '$scan_date'
                        },
                        'live_domain_count': {
                            '$sum': 1
                        },
                        'live_supports_https_count': {
                            '$sum': {
                                '$cond': [
                                    {
                                        '$eq': ['$domain_supports_https', True]
                                    },
                                    1,
                                    0
                                ]
                            }
                        },
                        'live_enforces_https_count': {
                            '$sum': {
                                '$cond': [
                                    {
                                        '$eq': ['$domain_enforces_https', True]
                                    },
                                    1,
                                    0
                                ]
                            }
                        },
                        'live_uses_strong_hsts_count': {
                            '$sum': {
                                '$cond': [
                                    {
                                        '$eq': ['$domain_uses_strong_hsts', True]
                                    },
                                    1,
                                    0
                                ]
                            }
                        },
                        'live_no_weak_crypto_count': {
                            '$sum': {
                                '$cond': [
                                    {
                                        '$eq': ['$has_weak_web_crypto', False]
                                    },
                                    1,
                                    0
                                ]
                            }
                        },
                        'live_bod1801_web_compliant_count': {
                            '$sum': {
                                '$cond': [
                                    {
                                        '$and': [
                                            {'$eq': ['$domain_supports_https', True]},
                                            {'$eq': ['$domain_enforces_https', True]},
                                            {'$eq': ['$domain_uses_strong_hsts', True]},
                                            {'$eq': ['$has_weak_web_crypto', False]}
                                        ]
                                    },
                                    1,
                                    0
                                ]
                            }
                        }
                    }
                },
                # Calculate how many live domains are not BOD 18-01
                # Compliant:
                {
                    '$project': {
                        '_id': 1,
                        'earliest_scan_date': 1,
                        'live_domain_count': 1,
                        'live_supports_https_count': 1,
                        'live_enforces_https_count': 1,
                        'live_uses_strong_hsts_count': 1,
                        'live_no_weak_crypto_count': 1,
                        'live_bod1801_web_compliant_count': 1,
                        'live_missing_https_hsts_count': {
                            '$subtract': [
                                '$live_domain_count',
                                '$live_uses_strong_hsts_count'
                            ]
                        }
                    }
                },
                {
                    '$sort': {
                        '_id': 1
                    }
                }
            ], cursor={})
        )

    def __run_sslyze_scan_queries(self, cybex_orgs):
        # Query sslyze-scans for weak crypto in domains (includes both web and email servers)
        # Used in 'BOD Results by Agency' section
        latest_cybex_hostnames = list(set(
            self.__results['latest_cybex_trustymail_base_domains_and_smtp_subdomains']
        ) | set(
            self.__results['latest_cybex_https_scan_live_hostnames']
        ))

        self.__results['sslyze-scan'] = list(self.__scan_db.sslyze_scan.aggregate(
            [
                {
                    '$match': {
                        'latest':True,
                        'domain': {
                            '$in':latest_cybex_hostnames
                        },
                        'scanned_port': {
                            '$in': [25, 587, 465, 443]
                        }
                    }
                },    # 25, 587, 465 = SMTP (email)    443 = HTTPS (web)
                {
                    '$project': {
                        'agency_id': '$agency.id',
                        'domain': '$domain',
                        'sslv2': '$sslv2',
                        'sslv3': '$sslv3',
                        'any_rc4': '$any_rc4',
                        'any_3des': '$any_3des'
                    }
                },
                {
                    '$group': {
                        '_id':{
                            'domain':'$domain',
                            'agency_id':'$agency_id'
                        },
                        'ports_with_weak_crypto_count': {
                            '$sum': {
                                '$cond': [
                                    {
                                        '$or': [
                                            {
                                                '$eq': ['$sslv2', True]
                                            },
                                            {
                                                '$eq': ['$sslv3', True]
                                            },
                                            {
                                                '$eq': ['$any_rc4', True]
                                            },
                                            {
                                                '$eq': ['$any_3des', True]
                                            }
                                        ]
                                    },
                                    1,
                                    0
                                ]
                            }
                        }
                    }
                },
                {
                    '$project': {
                        'domain': '$id.domain',
                        'agency_id': '$id.agency_id',
                        'domain_has_weak_crypto': {
                            '$cond': [
                                {
                                    '$gt': ['$ports_with_weak_crypto_count', 0]
                                },
                                True,
                                False
                            ]
                        }
                    }
                },
                {
                    '$group': {
                        '_id': '$_id.agency_id',
                        'domain_count': {'$sum': 1},
                        'domains_with_weak_crypto_count': {
                            '$sum': {
                                '$cond': [
                                    {
                                        '$eq': ['$domain_has_weak_crypto', True]
                                    },
                                    1,
                                    0
                                ]
                            }
                        }
                    }
                },
                {
                    '$sort': {'_id':1}
                }
            ], cursor={}
        ))

    def __create_domain_to_org_map(self, org_list):
        '''
        Map each domain (owned by an org in org_list) to the org that owns it
        '''

        domains = self.__scan_db.domains.find(
            {
                'agency.id': {'$in': org_list}
            },
            {
                '_id': True,
                'agency.id': True
            }
        )

        return {d['_id'].lower(): d['agency']['id'] for d in domains}

    def __accumulate_federal_cert_totals(self, certificate,
                                         field_to_accumulate):
        self.__results['federal_totals']['cert-scan'][field_to_accumulate] += 1

        if certificate['cfo_act_org']:
            self.__results['cfo_totals']['cert-scan'][field_to_accumulate] += 1

        if certificate['non_cfo_act_org']:
            self.__results['non_cfo_totals']['cert-scan'][
                field_to_accumulate] += 1

    def __create_cert_summary_by_org(self, certificates, current_fy_start):
        '''
        Build certificate summary results for each organization, as well as
        accumulate Federal/CFO/Non-CFO totals (they cannot simply be added up
        at the end, due to the possibility of a single cert applying to more
        than one organization).
        '''
        results = defaultdict(lambda: defaultdict(lambda: 0))
        # Initialize results with every org in our domain_to_org_map so we
        # can count them as being 'scanned' for certificates
        for org in set(self.__results['domain_to_org_map'].values()):
            for metric in ['unexpired_certs_count',
                           'certs_issued_current_fy_count',
                           'certs_issued_past_30_days_count',
                           'certs_issued_past_7_days_count']:
                           results[org][metric] = 0

        # Initialize federal totals (used for all metrics, not just cert-scan)
        for total_id in ['federal_totals', 'cfo_totals', 'non_cfo_totals']:
            self.__results[total_id] = dict()

            # initialize cert-scan metrics to 0
            self.__results[total_id]['cert-scan'] = {
                'unexpired_certs_count': 0,
                'certs_issued_current_fy_count': 0,
                'certs_issued_past_30_days_count': 0,
                'certs_issued_past_7_days_count': 0
            }

        for cert in certificates:
            cert['cfo_act_org'] = False
            cert['non_cfo_act_org'] = False
            orgs_owning_subjects = set()

            for subject in cert.get('trimmed_subjects'):
                org_id = self.__results['domain_to_org_map'].get(subject.lower())
                if org_id:
                    orgs_owning_subjects.add(org_id)

            # Reminder: It is possible for a cert to contain subjects for
            # *BOTH* CFO Act and Non-CFO Act orgs. This explains why
            # the total number of Federal certs can be less than the sum of
            # CFO Act and Non-CFO Act total certs.
            #
            # It is also possible for a cert to contain subjects for
            # multiple CFO Act or multiple Non-CFO Act orgs. This explains why
            # the total number of CFO Act/Non-CFO Act certs can be less than
            # the sum of certs for the agencies in those groups.
            #
            # Assumption: Every org in this Scorecard is a Federal org and
            # each one is either a "CFO Act" org or a "Non-CFO Act" org
            for org_id in orgs_owning_subjects:
                if org_id in self.__cfo_act_orgs:
                    cert['cfo_act_org'] = True
                else:
                    cert['non_cfo_act_org'] = True

            # Is cert unexpired?
            if cert['not_after'] > self.__generated_time:
                for org in orgs_owning_subjects:
                    results[org]['unexpired_certs_count'] += 1
                self.__accumulate_federal_cert_totals(
                    cert, 'unexpired_certs_count')

            # Was cert issued in this fiscal year?
            if cert['sct_or_not_before'] > current_fy_start:
                for org in orgs_owning_subjects:
                    results[org]['certs_issued_current_fy_count'] += 1
                self.__accumulate_federal_cert_totals(
                    cert, 'certs_issued_current_fy_count')

            # Was cert issued in the past 30 days?
            if cert['sct_or_not_before'] > self.__generated_time - timedelta(days=30):
                for org in orgs_owning_subjects:
                    results[org]['certs_issued_past_30_days_count'] += 1
                self.__accumulate_federal_cert_totals(
                    cert, 'certs_issued_past_30_days_count')

            # Was cert issued in the past 7 days?
            if cert['sct_or_not_before'] > self.__generated_time - timedelta(days=7):
                for org in orgs_owning_subjects:
                    results[org]['certs_issued_past_7_days_count'] += 1
                self.__accumulate_federal_cert_totals(
                    cert, 'certs_issued_past_7_days_count')

        return results

    def __run_cert_scan_queries(self, cybex_orgs):
        '''
        Fetch certificates that contain a subject that matches a domain
        belonging to any of the cybex_orgs AND that meet ANY of the
        following conditions:
           - Certificate is not expired
           - Certificate was issued in the current fiscal year
           - Certificate was issued in the past 30 days
        '''

        # Store domain_to_org_map in self.__results for later use
        self.__results['domain_to_org_map'] = self.__create_domain_to_org_map(cybex_orgs)

        # Create list of domains that will be used to capture the certs
        # for every org in cybex_orgs
        cybex_domains = self.__results['domain_to_org_map'].keys()

        current_fy_start = report_dates(now=self.__generated_time)['fy_start']

        relevant_certs = self.__scan_db.certs.find(
            {
                'trimmed_subjects': {
                    '$in': cybex_domains
                },
                '$or': [
                    {
                        'not_after': {
                            '$gt': self.__generated_time
                        }
                    },
                    {
                        'sct_or_not_before': {
                            '$gte': current_fy_start
                        }
                    },
                    {
                        'sct_or_not_before': {
                            '$gte': self.__generated_time - timedelta(days=30)
                        }
                    }
                ]
            },
            {
                '_id': False,
                'not_after': True,
                'trimmed_subjects': True,
                'sct_or_not_before': True
            }
        )

        self.__results['cert-scan'] = self.__create_cert_summary_by_org(relevant_certs,
                                                                        current_fy_start)

    def __run_queries(self):
        # Get cyhy request docs for all orgs that have CYBEX in their report_types
        self.__requests = list(self.__cyhy_db.RequestDoc.find(
            {
                'report_types': REPORT_TYPE.CYBEX
            }
        ))
        cybex_orgs = []
        for r in self.__requests:
            cybex_orgs.append(r['_id'])
            self.__all_cybex_orgs_with_descendants.append(r['_id'])
            self.__all_cybex_orgs_with_descendants += self.__cyhy_db.RequestDoc.get_all_descendants(r['_id'])

        # Build up list of CYBEX org tallies that were updated within past CURRENTLY_SCANNED_DAYS days
        for tally in list(self.__cyhy_db.TallyDoc.find({'_id': {'$in': cybex_orgs}})):
            if tally['last_change'] >= self.__generated_time - timedelta(days=CURRENTLY_SCANNED_DAYS):
                self.__tallies.append(tally)                # Append the tally if it's been changed recently
            else:       # Check if this org has any descendants with tallies that have been changed recently
                tally_descendant_orgs = self.__cyhy_db.RequestDoc.get_all_descendants(tally['_id'])
                if tally_descendant_orgs:
                    for tally_descendant in list(self.__cyhy_db.TallyDoc.find({'_id':{'$in':tally_descendant_orgs}})):
                        if tally_descendant['last_change'] >= self.__generated_time - timedelta(days=CURRENTLY_SCANNED_DAYS):
                            self.__tallies.append(tally)    # Append the top-level org's tally if the descendant has been changed recently
                            break                           # No need to check any other descendants

        # Get list of 'CFO Act' orgs
        if self.__cyhy_db.RequestDoc.find_one({'_id': 'FED_CFO_ACT'}):
            self.__cfo_act_orgs = self.__cyhy_db.RequestDoc.find_one({'_id': 'FED_CFO_ACT'})['children']
        else:
            self.__cfo_act_orgs = []

        # Run queries for each scanner
        self.__run_vuln_scan_queries(cybex_orgs)
        self.__run_trustymail_queries(cybex_orgs)
        self.__run_https_scan_queries(cybex_orgs)
        self.__run_sslyze_scan_queries(cybex_orgs)
        self.__run_cert_scan_queries(cybex_orgs)

    def __populate_scorecard_doc(self):
        # Go through each request doc and check if the org has a current tally doc
        for r in self.__requests:
            score = { 'vuln-scan': {'scanned':False,
                                    'metrics': {'open_criticals':0,
                                                'open_criticals_on_previous_scorecard':0,
                                                'open_criticals_0-7_days':0,
                                                'open_criticals_7-15_days':0,
                                                'open_criticals_15-30_days':0,
                                                'open_criticals_30-90_days':0,
                                                'open_criticals_more_than_90_days':0,
                                                'open_overdue_criticals': 0,
                                                'open_highs':0,
                                                'open_highs_on_previous_scorecard':0,
                                                'open_highs_0-7_days':0,
                                                'open_highs_7-15_days':0,
                                                'open_highs_15-30_days':0,
                                                'open_highs_30-90_days':0,
                                                'open_highs_more_than_90_days':0,
                                                'open_overdue_highs': 0,
                                                'addresses':0,
                                                'active_hosts':0}},
                      'trustymail': {'scanned':False,
                                     'base_domains': {'domain_count':0, 'live_domain_count':0,
                                                      'live_supports_starttls_count':0,
                                                      'live_supports_starttls_pct':0.0, 'live_supports_starttls_pct_str':'0.0%',
                                                      'all_live_supports_starttls':False,
                                                      'live_missing_starttls_count':0,
                                                      'live_valid_spf_count':0,
                                                      'live_valid_spf_pct':0.0, 'live_valid_spf_pct_str':'0.0%',
                                                      'live_spf_covered_count':0,
                                                      'live_spf_covered_pct':0.0, 'live_spf_covered_pct_str':'0.0%',
                                                      'all_live_spf_covered':False,
                                                      'live_valid_dmarc_count':0,
                                                      'live_valid_dmarc_pct':0.0, 'live_valid_dmarc_pct_str':'0.0%',
                                                      'all_live_dmarc_valid':False,
                                                      'live_dmarc_reject_count':0,
                                                      'live_dmarc_reject_pct':0.0, 'live_dmarc_reject_pct_str':'0.0%',
                                                      'all_live_dmarc_reject':False,
                                                      'live_has_bod1801_dmarc_uri_count':0,
                                                      'live_has_bod1801_dmarc_uri_pct':0.0, 'live_has_bod1801_dmarc_uri_pct_str':'0.0%',
                                                      'all_live_has_bod1801_dmarc_uri':False,
                                                      'dmarc_reject_none':False,
                                                      'dmarc_reject_some':False,
                                                      'dmarc_reject_all':False,
                                                      'live_bod1801_dmarc_compliant_count':0,
                                                      'live_bod1801_dmarc_non_compliant_count':0,
                                                      'live_no_weak_crypto_count':0,
                                                      'live_no_weak_crypto_pct':0, 'live_no_weak_crypto_pct_str':'0.0%',
                                                      'all_live_no_weak_crypto':False,
                                                      'live_bod1801_email_compliant_count':0,
                                                      'live_bod1801_email_compliant_pct':0.0, 'live_bod1801_email_compliant_pct_str':'0.0%',
                                                      'all_live_bod1801_email_compliant':False,
                                                      'live_bod1801_email_non_compliant_count':0},
                                     'base_domains_and_smtp_subdomains': {'domain_count':0, 'live_domain_count':0,
                                                      'live_supports_starttls_count':0,
                                                      'live_supports_starttls_pct':0.0, 'live_supports_starttls_pct_str':'0.0%',
                                                      'all_live_supports_starttls':False,
                                                      'live_missing_starttls_count':0,
                                                      'live_valid_spf_count':0,
                                                      'live_valid_spf_pct':0.0, 'live_valid_spf_pct_str':'0.0%',
                                                      'live_spf_covered_count':0,
                                                      'live_spf_covered_pct':0.0, 'live_spf_covered_pct_str':'0.0%',
                                                      'all_live_spf_covered':False,
                                                      'live_valid_dmarc_count':0,
                                                      'live_valid_dmarc_pct':0.0, 'live_valid_dmarc_pct_str':'0.0%',
                                                      'all_live_dmarc_valid':False,
                                                      'live_dmarc_reject_count':0,
                                                      'live_dmarc_reject_pct':0.0, 'live_dmarc_reject_pct_str':'0.0%',
                                                      'all_live_dmarc_reject':False,
                                                      'live_has_bod1801_dmarc_uri_count':0,
                                                      'live_has_bod1801_dmarc_uri_pct':0.0, 'live_has_bod1801_dmarc_uri_pct_str':'0.0%',
                                                      'all_live_has_bod1801_dmarc_uri':False,
                                                      'dmarc_reject_none':False,
                                                      'dmarc_reject_some':False,
                                                      'dmarc_reject_all':False,
                                                      'live_bod1801_dmarc_compliant_count':0,
                                                      'live_bod1801_dmarc_non_compliant_count':0,
                                                      'live_no_weak_crypto_count':0,
                                                      'live_no_weak_crypto_pct':0, 'live_no_weak_crypto_pct_str':'0.0%',
                                                      'all_live_no_weak_crypto':False,
                                                      'live_bod1801_email_compliant_count':0,
                                                      'live_bod1801_email_compliant_pct':0.0, 'live_bod1801_email_compliant_pct_str':'0.0%',
                                                      'all_live_bod1801_email_compliant':False,
                                                      'live_bod1801_email_non_compliant_count':0}},
                      'https-scan': {'scanned':False,
                                     'live_domains': {'live_domain_count':0,
                                                      'live_supports_https_count':0,
                                                      'live_supports_https_pct':0.0,
                                                      'live_supports_https_pct_str':'0.0%',
                                                      'all_live_supports_https':False,
                                                      'live_enforces_https_count':0,
                                                      'live_enforces_https_pct':0.0,
                                                      'live_enforces_https_pct_str':'0.0%',
                                                      'all_live_enforces_https':False,
                                                      'live_uses_strong_hsts_count':0,
                                                      'live_uses_strong_hsts_pct':0.0,
                                                      'live_uses_strong_hsts_pct_str':'0.0%',
                                                      'all_live_uses_strong_hsts':False,
                                                      'live_no_weak_crypto_count':0,
                                                      'live_no_weak_crypto_pct':0, 'live_no_weak_crypto_pct_str':'0.0%',
                                                      'all_live_no_weak_crypto':False,
                                                      'live_bod1801_web_compliant_count':0,
                                                      'live_bod1801_web_compliant_pct':0.0,
                                                      'live_bod1801_web_compliant_pct_str':'0.0%',
                                                      'all_live_bod1801_web_compliant':False,
                                                      'live_missing_https_hsts_count':0}},
                      'sslyze-scan': {'scanned':False,
                                      'live_domains': {'domain_count':0,
                                                       'live_no_weak_crypto_count':0,
                                                       'live_has_weak_crypto_count':0}},
                      'cert-scan': {'scanned':False,
                                    'metrics': {'unexpired_certs_count':0,
                                                'certs_issued_current_fy_count':0,
                                                'certs_issued_past_30_days_count':0,
                                                'certs_issued_past_7_days_count':0}}
                    }
            score['owner'] = r['_id']
            score['acronym'] = r['agency']['acronym']
            score['name'] = r['agency']['name']
            if r['_id'] in self.__cfo_act_orgs:
                score['cfo_act_org'] = True
            else:
                score['cfo_act_org'] = False

            # Was this organization granted a 3DES exception by CISA?
            score['3des_exception'] = score['acronym'] in self.__3des_exceptions

            # Pull trustymail results into the score
            for trustymail_result_set in ['base_domains', 'base_domains_and_smtp_subdomains']:
                for trustymail_result in self.__results['trustymail_' + trustymail_result_set]:
                    if trustymail_result['_id'] == score['owner']:  # Found info for the current org
                        score['trustymail']['scanned'] = True
                        for metric in ('domain_count', 'live_domain_count', 'live_valid_dmarc_count', 'live_dmarc_reject_count', 'live_has_bod1801_dmarc_uri_count', 'live_valid_spf_count', 'live_spf_covered_count', 'live_missing_starttls_count', 'live_no_weak_crypto_count', 'live_bod1801_dmarc_compliant_count', 'live_bod1801_email_compliant_count', 'live_supports_starttls_count', 'live_bod1801_dmarc_non_compliant_count', 'live_bod1801_email_non_compliant_count'):
                            score['trustymail'][trustymail_result_set][metric] = trustymail_result[metric]

                        # Calculate trustymail summary percentages
                        if trustymail_result['live_domain_count']:
                            current_live_domain_count = trustymail_result['live_domain_count']
                            for metric in ['live_valid_dmarc', 'live_dmarc_reject', 'live_has_bod1801_dmarc_uri', 'live_supports_starttls', 'live_spf_covered', 'live_no_weak_crypto', 'live_bod1801_email_compliant']:
                                current_metric_count = trustymail_result[metric + '_count']
                                score['trustymail'][trustymail_result_set][metric + '_pct'] = current_metric_count / float(current_live_domain_count)
                                score['trustymail'][trustymail_result_set][metric + '_pct_str'] = '{0:.1%}'.format(score['trustymail'][trustymail_result_set][metric + '_pct'])

                        # Check for perfect scores in each category to be displayed in the 'Results by Agency' section
                        for (score_percent, perfect_flag) in [('live_valid_dmarc_pct', 'all_live_dmarc_valid'),
                                                              ('live_dmarc_reject_pct', 'all_live_dmarc_reject'),
                                                              ('live_has_bod1801_dmarc_uri_pct', 'all_live_has_bod1801_dmarc_uri'),
                                                              ('live_supports_starttls_pct', 'all_live_supports_starttls'),
                                                              ('live_spf_covered_pct', 'all_live_spf_covered'),
                                                              ('live_no_weak_crypto_pct', 'all_live_no_weak_crypto'),
                                                              ('live_bod1801_email_compliant_pct', 'all_live_bod1801_email_compliant')]:
                            if score['trustymail'][trustymail_result_set][score_percent] == 1.0:
                                score['trustymail'][trustymail_result_set][perfect_flag] = True

                        # Set flag for DMARC policy of reject adoption
                        # (None, Some, All)
                        if trustymail_result['live_dmarc_reject_count'] == 0:
                            score['trustymail'][trustymail_result_set]['dmarc_reject_none'] = True
                            # Special case: Only do this for base_domains_and_smtp_subdomains
                            if trustymail_result_set == 'base_domains_and_smtp_subdomains':
                                self.__dmarc_reject_none.append({'acronym':score['owner'], 'cfo_act_org':score['cfo_act_org']})
                        elif trustymail_result['live_dmarc_reject_count'] == trustymail_result['live_domain_count']:
                            score['trustymail'][trustymail_result_set]['dmarc_reject_all'] = True
                            # Special case: Only do this for base_domains_and_smtp_subdomains
                            if trustymail_result_set == 'base_domains_and_smtp_subdomains':
                                self.__dmarc_reject_all.append({'acronym':score['owner'], 'cfo_act_org':score['cfo_act_org']})
                        else:
                            score['trustymail']['dmarc_reject_some'] = True
                            # Special case: Only do this for base_domains_and_smtp_subdomains
                            if trustymail_result_set == 'base_domains_and_smtp_subdomains':
                                self.__dmarc_reject_some.append({'acronym':score['owner'], 'cfo_act_org':score['cfo_act_org']})
                        break

            # Pull https-scan results into the score
            for https_scan_result in self.__results['https-scan']:
                if https_scan_result['_id'] == score['owner']:  # Found info for the current org
                    score['https-scan']['scanned'] = True
                    for metric in ('live_domain_count', 'live_supports_https_count', 'live_enforces_https_count', 'live_uses_strong_hsts_count', 'live_no_weak_crypto_count', 'live_bod1801_web_compliant_count', 'live_missing_https_hsts_count'):
                        score['https-scan']['live_domains'][metric] = https_scan_result[metric]

                    # Calculate https-scan percentages
                    if https_scan_result['live_domain_count']:
                        current_live_domain_count = https_scan_result['live_domain_count']
                        for metric in ['live_supports_https', 'live_enforces_https', 'live_uses_strong_hsts', 'live_no_weak_crypto', 'live_bod1801_web_compliant']:
                            current_metric_count = https_scan_result[metric + '_count']
                            score['https-scan']['live_domains'][metric + '_pct'] = current_metric_count / float(current_live_domain_count)
                            score['https-scan']['live_domains'][metric + '_pct_str'] = '{0:.1%}'.format(score['https-scan']['live_domains'][metric + '_pct'])

                    # Check for perfect scores in each category to be displayed in the 'Results by Agency' section
                    for (score_percent, perfect_flag) in [('live_supports_https_pct', 'all_live_supports_https'),
                                                          ('live_enforces_https_pct', 'all_live_enforces_https'),
                                                          ('live_uses_strong_hsts_pct', 'all_live_uses_strong_hsts'),
                                                          ('live_no_weak_crypto_pct', 'all_live_no_weak_crypto'),
                                                          ('live_bod1801_web_compliant_pct', 'all_live_bod1801_web_compliant')]:
                        if score['https-scan']['live_domains'][score_percent] == 1.0:
                            score['https-scan']['live_domains'][perfect_flag] = True

                    # Add org to appropriate list for Strong HSTS adoption
                    # (None, Some, All)
                    if https_scan_result['live_uses_strong_hsts_count'] == 0:
                        self.__strong_hsts_none.append(
                            {'acronym': score['owner'],
                             'cfo_act_org': score['cfo_act_org']})
                    elif https_scan_result[
                      'live_uses_strong_hsts_count'] == https_scan_result[
                      'live_domain_count']:
                        self.__strong_hsts_all.append(
                            {'acronym': score['owner'],
                             'cfo_act_org': score['cfo_act_org']})
                    else:
                        self.__strong_hsts_some.append(
                            {'acronym': score['owner'],
                             'cfo_act_org': score['cfo_act_org']})
                    break

            # Pull sslyze-scan results into the score
            for sslyze_scan_result in self.__results['sslyze-scan']:
                if sslyze_scan_result['_id'] == score['owner']:  # Found info for the current org
                    score['sslyze-scan']['scanned'] = True
                    score['sslyze-scan']['live_domains']['domain_count'] = sslyze_scan_result['domain_count']
                    score['sslyze-scan']['live_domains']['live_has_weak_crypto_count'] = sslyze_scan_result['domains_with_weak_crypto_count']
                    score['sslyze-scan']['live_domains']['live_no_weak_crypto_count'] = sslyze_scan_result['domain_count'] - sslyze_scan_result['domains_with_weak_crypto_count']
                    break

            # Pull cert-scan results into the score
            if self.__results['cert-scan'].get(score['owner']):  # Found info for the current org
                score['cert-scan']['scanned'] = True
                for metric in ['unexpired_certs_count',
                              'certs_issued_current_fy_count',
                              'certs_issued_past_30_days_count',
                              'certs_issued_past_7_days_count']:
                    score['cert-scan']['metrics'][metric] = self.__results['cert-scan'][score['owner']][metric]

                if score['cert-scan']['metrics']['certs_issued_past_7_days_count'] > 0:
                    self.__orgs_with_recently_issued_certs.append(score)
                else:
                    self.__orgs_with_no_recently_issued_certs.append(score)
            else:
                self.__orgs_with_no_known_domains.append(score)

            # Pull vuln-scan results into the score
            for t in self.__tallies:
                if t['_id'] == r['_id']:  # Found a current CyHy tally that matches this request (org)
                    # currentlyScanned = True
                    score['vuln-scan']['scanned'] = True
                    for i in self.__previous_scorecard_data['all_orgs_alpha']:
                        if i['owner'] == score['owner']:  # Found info for the current org
                            if i['vuln-scan']['metrics'].get('open_criticals'):
                                score['vuln-scan']['metrics']['open_criticals_on_previous_scorecard'] = i['vuln-scan']['metrics']['open_criticals']

                            if i['vuln-scan']['metrics'].get('open_highs'):
                                score['vuln-scan']['metrics']['open_highs_on_previous_scorecard'] = i['vuln-scan']['metrics']['open_highs']
                            break

                    # Search through CyHy query results for data from the current org and add it to the current score
                    for vuln_scan_result in self.__results['vuln-scan']['open_critical_ticket_counts']:
                        if vuln_scan_result['_id']['owner'] == score['owner']:  # Found info for the current org
                            for score_field, result_field in [
                            ('open_criticals', 'open_tix_count'),
                            ('open_criticals_0-7_days', 'open_tix_opened_less_than_7_days_ago'),
                            ('open_criticals_7-15_days', 'open_tix_opened_7-15_days_ago'),
                            ('open_criticals_15-30_days', 'open_tix_opened_15-30_days_ago'),
                            ('open_criticals_30-90_days', 'open_tix_opened_30-90_days_ago'),
                            ('open_criticals_more_than_90_days', 'open_tix_opened_more_than_90_days_ago'),
                            ('open_overdue_criticals', 'open_overdue_tix')]:
                                score['vuln-scan']['metrics'][score_field] = vuln_scan_result[result_field]
                            break

                    for vuln_scan_result in self.__results['vuln-scan']['open_high_ticket_counts']:
                        if vuln_scan_result['_id']['owner'] == score['owner']:  # Found info for the current org
                            for score_field, result_field in [
                            ('open_highs', 'open_tix_count'),
                            ('open_highs_0-7_days', 'open_tix_opened_less_than_7_days_ago'),
                            ('open_highs_7-15_days', 'open_tix_opened_7-15_days_ago'),
                            ('open_highs_15-30_days', 'open_tix_opened_15-30_days_ago'),
                            ('open_highs_30-90_days', 'open_tix_opened_30-90_days_ago'),
                            ('open_highs_more_than_90_days', 'open_tix_opened_more_than_90_days_ago'),
                            ('open_overdue_highs', 'open_overdue_tix')]:
                                score['vuln-scan']['metrics'][score_field] = vuln_scan_result[result_field]
                            break

                    for (result_field, score_field, data_key) in [('addresses', 'addresses', 'addresses_count'), ('active_hosts', 'active_hosts', 'active_hosts_count')]:
                        for i in self.__results['vuln-scan'][result_field]:
                            if i['_id']['owner'] == score['owner']:  # Found info for the current org
                                score['vuln-scan']['metrics'][score_field] = i[data_key]
                                break

                    # Fields calculated from info retrieved above
                    score['vuln-scan']['metrics']['open_criticals'] = score['vuln-scan']['metrics']['open_criticals'] - score['vuln-scan']['metrics']['open_criticals_0-7_days'] # Hack for CYHY-441; exclude criticals less than 7 days old from the open_criticals total
                    score['vuln-scan']['metrics']['open_criticals_delta_since_last_scorecard'] = score['vuln-scan']['metrics']['open_criticals'] - score['vuln-scan']['metrics']['open_criticals_on_previous_scorecard']

                    score['vuln-scan']['metrics']['open_highs'] = score['vuln-scan']['metrics']['open_highs'] - score['vuln-scan']['metrics']['open_highs_0-7_days'] # Hack for CYHY-441; exclude criticals less than 7 days old from the open_criticals total
                    score['vuln-scan']['metrics']['open_highs_delta_since_last_scorecard'] = score['vuln-scan']['metrics']['open_highs'] - score['vuln-scan']['metrics']['open_highs_on_previous_scorecard']

                    # Add org's score to appropriate list
                    org_has_criticals = False
                    if score['vuln-scan']['metrics'].get('open_criticals'):
                        self.__orgs_with_criticals.append(score)
                        org_has_criticals = True

                    if score['vuln-scan']['metrics'].get('open_highs'):
                        self.__orgs_with_highs.append(score)
                    elif not org_has_criticals:
                        self.__orgs_without_criticals_or_highs.append(score)

                    # Add current org's score to master list of scores
                    self.__scorecard_doc['scores'].append(score)
                    break

            if score['vuln-scan']['scanned'] == False:
                # Went through all tallies and didn't find a matching org for this request doc
                self.__orgs_not_vuln_scanned.append(score)
                self.__scorecard_doc['scores'].append(score)

    def __calculate_federal_totals(self):
        # Build Federal/CFO Act/Non-CFO Act totals
        for total_id in ['federal_totals', 'cfo_totals', 'non_cfo_totals']:
            # initialize vuln-scan metrics to 0
            self.__results[total_id]['vuln-scan'] = {'metrics': {'open_criticals':0, 'open_criticals_on_previous_scorecard':0, 'open_criticals_0-7_days':0, 'open_criticals_7-15_days':0, 'open_criticals_15-30_days':0, 'open_criticals_30-90_days':0, 'open_criticals_more_than_90_days':0, 'open_overdue_criticals':0, 'open_highs':0, 'open_highs_on_previous_scorecard':0, 'open_highs_0-7_days':0, 'open_highs_7-15_days':0, 'open_highs_15-30_days':0, 'open_highs_30-90_days':0, 'open_highs_more_than_90_days':0, 'open_overdue_highs':0, 'addresses':0, 'active_hosts':0}}

            # initialize trustymail metrics to 0
            self.__results[total_id]['trustymail'] = dict()
            for trustymail_result_set in ['base_domains', 'base_domains_and_smtp_subdomains']:
                self.__results[total_id]['trustymail'][trustymail_result_set] = {'domain_count':0, 'live_domain_count':0, 'live_valid_dmarc_count':0, 'live_dmarc_reject_count':0, 'live_has_bod1801_dmarc_uri_count':0, 'live_bod1801_dmarc_compliant_count':0, 'live_bod1801_dmarc_non_compliant_count':0, 'live_supports_starttls_count':0, 'live_missing_starttls_count':0, 'live_valid_spf_count':0, 'live_spf_covered_count':0, 'live_no_weak_crypto_count':0, 'live_bod1801_email_compliant_count':0, 'live_bod1801_email_non_compliant_count':0}

            # initialize https-scan metrics to 0
            self.__results[total_id]['https-scan'] = {'live_domains': {'live_domain_count':0, 'live_supports_https_count':0, 'live_enforces_https_count':0, 'live_uses_strong_hsts_count':0, 'live_no_weak_crypto_count':0, 'live_bod1801_web_compliant_count':0, 'live_missing_https_hsts_count':0}}

            # initialize sslyze-scan metrics to 0
            self.__results[total_id]['sslyze-scan'] = {'live_domains': {'domain_count':0, 'live_no_weak_crypto_count':0, 'live_has_weak_crypto_count':0}}

        # Accumulate all metrics (except for previously-handled cert-scan
        # metrics) into each totals dict
        for org in self.__scorecard_doc['scores']:
            for (scanner, scan_subtype, field) in [('vuln-scan', 'metrics', 'open_criticals'),
                                                   ('vuln-scan', 'metrics', 'open_criticals_on_previous_scorecard'),
                                                   ('vuln-scan', 'metrics', 'open_criticals_0-7_days'),
                                                   ('vuln-scan', 'metrics', 'open_criticals_7-15_days'),
                                                   ('vuln-scan', 'metrics', 'open_criticals_15-30_days'),
                                                   ('vuln-scan', 'metrics', 'open_criticals_30-90_days'),
                                                   ('vuln-scan', 'metrics', 'open_criticals_more_than_90_days'),
                                                   ('vuln-scan', 'metrics', 'open_overdue_criticals'),
                                                   ('vuln-scan', 'metrics', 'open_highs'),
                                                   ('vuln-scan', 'metrics', 'open_highs_on_previous_scorecard'),
                                                   ('vuln-scan', 'metrics', 'open_highs_0-7_days'),
                                                   ('vuln-scan', 'metrics', 'open_highs_7-15_days'),
                                                   ('vuln-scan', 'metrics', 'open_highs_15-30_days'),
                                                   ('vuln-scan', 'metrics', 'open_highs_30-90_days'),
                                                   ('vuln-scan', 'metrics', 'open_highs_more_than_90_days'),
                                                   ('vuln-scan', 'metrics', 'open_overdue_highs'),
                                                   ('vuln-scan', 'metrics', 'addresses'),
                                                   ('vuln-scan', 'metrics', 'active_hosts'),
                                                   ('trustymail', 'base_domains', 'domain_count'),
                                                   ('trustymail', 'base_domains', 'live_domain_count'),
                                                   ('trustymail', 'base_domains', 'live_valid_dmarc_count'),
                                                   ('trustymail', 'base_domains', 'live_dmarc_reject_count'),
                                                   ('trustymail', 'base_domains', 'live_has_bod1801_dmarc_uri_count'),
                                                   ('trustymail', 'base_domains', 'live_bod1801_dmarc_compliant_count'),
                                                   ('trustymail', 'base_domains', 'live_bod1801_dmarc_non_compliant_count'),
                                                   ('trustymail', 'base_domains', 'live_supports_starttls_count'),
                                                   ('trustymail', 'base_domains', 'live_missing_starttls_count'),
                                                   ('trustymail', 'base_domains', 'live_valid_spf_count'),
                                                   ('trustymail', 'base_domains', 'live_no_weak_crypto_count'),
                                                   ('trustymail', 'base_domains', 'live_bod1801_email_compliant_count'),
                                                   ('trustymail', 'base_domains', 'live_bod1801_email_non_compliant_count'),
                                                   ('trustymail', 'base_domains_and_smtp_subdomains', 'domain_count'),
                                                   ('trustymail', 'base_domains_and_smtp_subdomains', 'live_domain_count'),
                                                   ('trustymail', 'base_domains_and_smtp_subdomains', 'live_valid_dmarc_count'),
                                                   ('trustymail', 'base_domains_and_smtp_subdomains', 'live_dmarc_reject_count'),
                                                   ('trustymail', 'base_domains_and_smtp_subdomains', 'live_has_bod1801_dmarc_uri_count'),
                                                   ('trustymail', 'base_domains_and_smtp_subdomains', 'live_bod1801_dmarc_compliant_count'),
                                                   ('trustymail', 'base_domains_and_smtp_subdomains', 'live_bod1801_dmarc_non_compliant_count'),
                                                   ('trustymail', 'base_domains_and_smtp_subdomains', 'live_supports_starttls_count'),
                                                   ('trustymail', 'base_domains_and_smtp_subdomains', 'live_missing_starttls_count'),
                                                   ('trustymail', 'base_domains_and_smtp_subdomains', 'live_valid_spf_count'),
                                                   ('trustymail', 'base_domains_and_smtp_subdomains', 'live_spf_covered_count'),
                                                   ('trustymail', 'base_domains_and_smtp_subdomains', 'live_no_weak_crypto_count'),
                                                   ('trustymail', 'base_domains_and_smtp_subdomains', 'live_bod1801_email_compliant_count'),
                                                   ('trustymail', 'base_domains_and_smtp_subdomains', 'live_bod1801_email_non_compliant_count'),
                                                   ('https-scan', 'live_domains', 'live_domain_count'),
                                                   ('https-scan', 'live_domains', 'live_supports_https_count'),
                                                   ('https-scan', 'live_domains', 'live_enforces_https_count'),
                                                   ('https-scan', 'live_domains', 'live_uses_strong_hsts_count'),
                                                   ('https-scan', 'live_domains', 'live_no_weak_crypto_count'),
                                                   ('https-scan', 'live_domains', 'live_bod1801_web_compliant_count'),
                                                   ('https-scan', 'live_domains', 'live_missing_https_hsts_count'),
                                                   ('sslyze-scan', 'live_domains', 'domain_count'),
                                                   ('sslyze-scan', 'live_domains', 'live_no_weak_crypto_count'),
                                                   ('sslyze-scan', 'live_domains', 'live_has_weak_crypto_count')]:
                self.__results['federal_totals'][scanner][scan_subtype][field] += org[scanner][scan_subtype][field]
                if org['cfo_act_org']:
                    self.__results['cfo_totals'][scanner][scan_subtype][field] += org[scanner][scan_subtype][field]
                else:
                    self.__results['non_cfo_totals'][scanner][scan_subtype][field] += org[scanner][scan_subtype][field]

        for total_id in ['federal_totals', 'cfo_totals', 'non_cfo_totals']:
            # Calculate open_criticals_delta_since_last_scorecard
            self.__results[total_id]['vuln-scan']['metrics']['open_criticals_delta_since_last_scorecard'] = (self.__results[total_id]['vuln-scan']['metrics']['open_criticals'] - self.__results[total_id]['vuln-scan']['metrics']['open_criticals_on_previous_scorecard'])

            # Calculate open_highs_delta_since_last_scorecard
            if self.__results[total_id]['vuln-scan']['metrics'].get(
              'open_highs_on_previous_scorecard'):
                self.__results[total_id]['vuln-scan']['metrics']['open_highs_delta_since_last_scorecard'] = self.__results[total_id]['vuln-scan']['metrics']['open_highs'] - self.__results[total_id]['vuln-scan']['metrics']['open_highs_on_previous_scorecard']
            else:
                self.__results[total_id]['vuln-scan']['metrics']['open_highs_delta_since_last_scorecard'] = self.__results[total_id]['vuln-scan']['metrics']['open_highs']

            # Store various versions of trustymail & https-scan summary percentages for later use
            for (scanner, scan_subtype, metric) in [('trustymail', 'base_domains', 'live_supports_starttls'),
                                                    ('trustymail', 'base_domains', 'live_valid_spf'),
                                                    ('trustymail', 'base_domains', 'live_valid_dmarc'),
                                                    ('trustymail', 'base_domains', 'live_dmarc_reject'),
                                                    ('trustymail', 'base_domains', 'live_has_bod1801_dmarc_uri'),
                                                    ('trustymail', 'base_domains', 'live_no_weak_crypto'),
                                                    ('trustymail', 'base_domains', 'live_bod1801_email_compliant'),
                                                    ('trustymail', 'base_domains_and_smtp_subdomains', 'live_supports_starttls'),
                                                    ('trustymail', 'base_domains_and_smtp_subdomains', 'live_valid_spf'),
                                                    ('trustymail', 'base_domains_and_smtp_subdomains', 'live_spf_covered'),
                                                    ('trustymail', 'base_domains_and_smtp_subdomains', 'live_valid_dmarc'),
                                                    ('trustymail', 'base_domains_and_smtp_subdomains', 'live_has_bod1801_dmarc_uri'),
                                                    ('trustymail', 'base_domains_and_smtp_subdomains', 'live_dmarc_reject'),
                                                    ('trustymail', 'base_domains_and_smtp_subdomains', 'live_no_weak_crypto'),
                                                    ('trustymail', 'base_domains_and_smtp_subdomains', 'live_bod1801_email_compliant'),
                                                    ('https-scan', 'live_domains', 'live_supports_https'),
                                                    ('https-scan', 'live_domains', 'live_enforces_https'),
                                                    ('https-scan', 'live_domains', 'live_uses_strong_hsts'),
                                                    ('https-scan', 'live_domains', 'live_no_weak_crypto'),
                                                    ('https-scan', 'live_domains', 'live_bod1801_web_compliant')]:
                current_metric_count = self.__results[total_id][scanner][scan_subtype][metric + '_count']
                current_live_domain_count = self.__results[total_id][scanner][scan_subtype]['live_domain_count']
                if current_live_domain_count:
                    self.__results[total_id][scanner][scan_subtype][metric + '_pct'] = current_metric_count / float(current_live_domain_count)
                    self.__results[total_id][scanner][scan_subtype][metric + '_pct_str'] = '{0:.1%}'.format(self.__results[total_id][scanner][scan_subtype][metric + '_pct'])
                    self.__results[total_id][scanner][scan_subtype][metric + '_pct_int'] = int(round(current_metric_count / float(current_live_domain_count) * 100)) # In python 3, int/float stuff won't be needed
                else:
                    self.__results[total_id][scanner][scan_subtype][metric + '_pct'] = 0.0
                    self.__results[total_id][scanner][scan_subtype][metric + '_pct_str'] = '0.0%'
                    self.__results[total_id][scanner][scan_subtype][metric + '_pct_int'] = 0

    def __make_fake_agency(self, real_agencies, real_acronyms, fake_agencies, fake_acronyms):
        FIRST = ['American', 'Atlantic', 'Central', 'Civil', 'Eastern American', 'Executive', 'Federal', 'Foreign', 'General', 'Government', 'Interstate', 'International', 'Midwest', 'National', 'North American', 'Overseas', 'Pacific', 'Regional', 'State', 'Western American', 'United States']
        SECOND = ['Agriculture', 'Art', 'Airport', 'Business', 'Commerce', 'Communication', 'Development', 'Economic', 'Education', 'Election', 'Energy', 'Environment', 'Finance', 'Gaming', 'Health', 'Housing', 'Infrastructure', 'Industrial', 'Insurance', 'Justice', 'Labor', 'Land', 'Maritime', 'Management', 'Natural Resources', 'Nuclear', 'Planning', 'Policy', 'Protection', 'Records', 'Resource', 'Regulatory', 'Retirement', 'Safety', 'Science', 'Security', 'Space', 'Trade', 'Transportation', 'Water']
        THIRD = ['Administration', 'Advisory Council', 'Agency', 'Authority', 'Bureau', 'Board', 'Center', 'Commission', 'Corporation', 'Corps', 'Council', 'Department', 'Enforcement', 'Foundation', 'Inquisition', 'Institute', 'Institutes', 'Laboratories', 'Office', 'Program', 'Regulatory Commission', 'Review Board', 'Service', 'Services', 'Trust']
        bad_acronyms = ['ASS', 'PIS', 'FARC']

        acceptable_name = False
        while not acceptable_name:
            fake_name = random.choice(FIRST) + ' ' + random.choice(SECOND) + ' ' + random.choice(THIRD)
            fake_acronym = "".join(c[0] for c in fake_name.split())
            if (fake_name not in real_agencies + fake_agencies) and (fake_acronym not in real_acronyms + fake_acronyms + bad_acronyms):
                 acceptable_name = True
        return fake_name, fake_acronym

    def __anonymize_scorecard(self):
        real_agency_names = []
        real_agency_acronyms = []
        fake_agency_names = []
        fake_agency_acronyms = []
        real_to_fake_lookup = dict()

        for r in self.__requests:
            real_agency_names.append(r['agency']['name'])
            real_agency_acronyms.append(r['agency']['acronym'])

        for score_list in [self.__scorecard_doc['scores'],
                           self.__strong_hsts_all,
                           self.__strong_hsts_some,
                           self.__strong_hsts_none,
                           self.__dmarc_reject_all,
                           self.__dmarc_reject_some,
                           self.__dmarc_reject_none]:
            for s in score_list:
                # If we already have a fake acronym for this org, use it; otherwise create a new one
                real_acronym = s['acronym']
                if real_to_fake_lookup.get(real_acronym):
                    s['acronym'] = real_to_fake_lookup[real_acronym]['fake_acronym']
                    s['owner'] = real_to_fake_lookup[real_acronym]['fake_acronym']
                    s['name'] = real_to_fake_lookup[real_acronym]['fake_name']
                else:
                    fake_agency_name, fake_agency_acronym = self.__make_fake_agency(real_agency_names, real_agency_acronyms,
                                                                                    fake_agency_names, fake_agency_acronyms)
                    fake_agency_names.append(fake_agency_name)
                    fake_agency_acronyms.append(fake_agency_acronym)
                    real_to_fake_lookup[s['acronym']] = {'fake_acronym':fake_agency_acronym, 'fake_name':fake_agency_name}
                    s['acronym'] = fake_agency_acronym
                    s['owner'] = fake_agency_acronym
                    s['name'] = fake_agency_name

    def generate_cybex_scorecard(self):
        print ' running DB queries'
        # access database and cache results
        self.__run_queries()

        print ' parsing data'
        # build up the scorecard_doc from the query results
        self.__populate_scorecard_doc()
        # calculate federal/cfo act/non-cfo act totals
        self.__calculate_federal_totals()

        # anonymize data if requested
        if self.__anonymize:
            self.__anonymize_scorecard()
            self.__log_scorecard_to_db = False  # Don't log creation of anonymous scorecards to the DB
            self.__results['scorecard_name'] = 'SAMPLE'
            # self.__results['scorecard_subset_name'] = 'Subset XYZ'
        else:
            self.__results['scorecard_name'] = 'Federal'
            # self.__results['scorecard_subset_name'] = ''

        # sort org lists
        for org_list in [self.__scorecard_doc['scores'],
                         self.__orgs_with_recently_issued_certs,
                         self.__orgs_with_no_recently_issued_certs,
                         self.__orgs_with_no_known_domains,
                         self.__orgs_with_criticals,
                         self.__orgs_with_highs,
                         self.__orgs_without_criticals_or_highs,
                         self.__orgs_not_vuln_scanned,
                         self.__strong_hsts_all,
                         self.__strong_hsts_some,
                         self.__strong_hsts_none,
                         self.__dmarc_reject_all,
                         self.__dmarc_reject_some,
                         self.__dmarc_reject_none]:
            org_list.sort(key=lambda x: x['acronym'])

        # create a working directory
        original_working_dir = os.getcwdu()
        if self.__debug:
            temp_working_dir = tempfile.mkdtemp(dir=original_working_dir)
        else:
            temp_working_dir = tempfile.mkdtemp()
        os.chdir(temp_working_dir)

        # setup the working directory
        self.__setup_work_directory(temp_working_dir)

        print ' generating attachments'
        # generate attachments
        self.__generate_attachments()

        print ' generating charts'
        # generate chart PDFs
        self.__generate_charts()

        # generate json input to mustache
        self.__generate_mustache_json(REPORT_JSON)

        # generate latex json + mustache
        self.__generate_latex(MUSTACHE_FILE, REPORT_JSON, REPORT_TEX)

        print ' assembling PDF'
        # generate report figures + latex
        self.__generate_final_pdf()

        # revert working directory
        os.chdir(original_working_dir)

        # copy report and json file to original working directory
        # and delete working directory
        if not self.__debug:
            src_filename = os.path.join(temp_working_dir, REPORT_PDF)
            timestamp = self.__generated_time.isoformat().replace(':','').split('.')[0]
            dest_filename = self.__results['scorecard_name'] + '_Cyber_Exposure_Scorecard-%s.pdf' % (timestamp)
            shutil.move(src_filename, dest_filename)
            src_filename = os.path.join(temp_working_dir, REPORT_JSON)
            timestamp = self.__generated_time.isoformat().replace(':','').split('.')[0]
            dest_filename = 'cybex_scorecard_%s.json' % (timestamp)
            shutil.move(src_filename, dest_filename)
            shutil.rmtree(temp_working_dir)

        if self.__log_scorecard_to_db:
            # add a doc to reports collection to log that this scorecard was generated
            self.__log_scorecard_report()

        return self.__results

    def __setup_work_directory(self, work_dir):
        me = os.path.realpath(__file__)
        my_dir = os.path.dirname(me)
        for n in (MUSTACHE_FILE,):
            file_src = os.path.join(my_dir, n)
            file_dst = os.path.join(work_dir, n)
            shutil.copyfile(file_src, file_dst)
        # copy static assets
        dir_src = os.path.join(my_dir, ASSETS_DIR_SRC)
        dir_dst = os.path.join(work_dir, ASSETS_DIR_DST)
        shutil.copytree(dir_src,dir_dst)

    ###############################################################################
    # Utilities
    ###############################################################################

    def __latex_escape(self, to_escape):
        return ''.join([LATEX_ESCAPE_MAP.get(i,i) for i in to_escape])

    def __latex_escape_structure(self, data):
        '''assumes that all sequences contain dicts'''
        if isinstance(data, dict):
            for k,v in data.items():
                if k.endswith('_tex'): # skip special tex values
                    continue
                if isinstance(v, basestring):
                    data[k] = self.__latex_escape(v)
                else:
                    self.__latex_escape_structure(v)
        elif isinstance(data, (list, tuple)):
            for i in data:
                self.__latex_escape_structure(i)

    def led(self, data):
        self.__latex_escape_dict(data)

    def __convert_levels_to_text(self, data, field):
        for row in data:
            row[field] = SEVERITY_LEVELS[row[field]]

    def __level_keys_to_text(self, data, lowercase=False):
        result = {}
        for k,v in data.items():
            if lowercase:
                new_key = SEVERITY_LEVELS[k].lower()
            else:
                new_key = SEVERITY_LEVELS[k]
            result[new_key] = v
        return result

    def __join_lists(self, data, field, joiner):
        for row in data:
            row[field] = joiner.join([str(i) for i in row[field]])

    def __replace_infinities(self, data, field, replacement):
        for row in data:
            if np.isinf(row[field]):
                row[field] = replacement

    def __dataframe_to_dicts(self, df, keep_index=False):
        df2 = df.reset_index().T.to_dict()
        result = df2.values()
        if not keep_index:
            for i in result:
                del(i['index'])
        return result

    def __percent_change(self, previous, current):
        if previous == 0:
            return '-'
        change = 100 * current / previous - 100
        change = round(change, 1)
        return change

    def __to_oxford_list(self, items, verb_single='', verb_muliple=''):
        if len(items) == 0:
            return None
        if len(items) == 1:
            return items[0] + verb_single
        if len(items) == 2:
            return '%s and %s%s' % (items[0], items[-1], verb_muliple)
        return ', '.join(items[:-1]) + ', and ' + items[-1] + verb_muliple

    def __udf_calc(self, preposition, v1, v2):
        if v2 > v1:
            return {'%s_up' % preposition : (v2-v1), '%s_up_flag' % preposition : True}
        if v1 > v2:
            return {'%s_down' % preposition : (v1-v2), '%s_down_flag' % preposition : True}
        assert v1 == v2, 'Glitch in the matrix!  Expected values to be equal.  Something has changed!'
        return {'%s_flat' % preposition : v1, '%s_flat_flag' % preposition : True}

    def __best_scale(self, df):
        '''determine of a line chart scale should be log or linear'''
        de = df.describe().T
        diff = de['mean'] - de['50%']
        max_diff = max(diff)
        if max_diff > 1000:
            return 'log'
        else:
            return 'linear'

    def __brief(self, labels):
        '''shrink labels for a pie chart'''
        results = list()
        for label in labels:
            label = label.replace('_',' ').strip()
            results.append(' '.join(label.split()[:5]))
        return results

    ###############################################################################
    #  Attachment Generation
    ###############################################################################
    def __generate_attachments(self):
        self.__generate_ed1901_results_by_agency_attachment()
        self.__generate_email_security_summary_attachment()
        self.__generate_bod_results_by_agency_attachment()
        self.__generate_bod1501_results_by_agency_attachment()
        self.__generate_web_security_results_by_agency_attachment()
        self.__generate_email_security_results_by_agency_attachment()

    def __generate_ed1901_results_by_agency_attachment(self):
        header_fields = ('acronym', 'name', 'cfo_act', 'unexpired_certificates', 'new_certificates_current_fiscal_year', 'new_certificates_past_30_days', 'new_certificates_past_7_days')
        data_fields = ('acronym', 'name', 'cfo_act_org', 'unexpired_certs_count', 'certs_issued_current_fy_count', 'certs_issued_past_30_days_count', 'certs_issued_past_7_days_count')
        with open(ED1901_RESULTS_BY_AGENCY_CSV_FILE, 'wb') as out_file:
            header_writer = csv.DictWriter(out_file, header_fields, extrasaction='ignore', quoting=csv.QUOTE_MINIMAL)
            data_writer = csv.DictWriter(out_file, data_fields, extrasaction='ignore', quoting=csv.QUOTE_MINIMAL)
            header_writer.writeheader()
            for org in copy.deepcopy(self.__scorecard_doc['scores']):
                for cert_scan_key in ('unexpired_certs_count', 'certs_issued_current_fy_count', 'certs_issued_past_30_days_count', 'certs_issued_past_7_days_count'):
                    if org['cert-scan']['scanned']:
                        org[cert_scan_key] = org['cert-scan']['metrics'].get(cert_scan_key)
                    else:
                        org[cert_scan_key] = 'N/A'
                data_writer.writerow(org)

    def __generate_email_security_summary_attachment(self):
        trustymail_dmarc_summary = sorted(self.__results['trustymail_dmarc_summary'], key=lambda x:x['_id'])
        with open(EMAIL_SECURITY_SUMMARY_CSV_FILE, 'wb') as out_file:
            data_writer = csv.writer(out_file, delimiter=',')
            # build and write CSV header
            header_row = ['dmarc_policy']
            for summary_item in trustymail_dmarc_summary:
                header_row.append(summary_item['_id'].strftime('%Y-%m-%d'))
            data_writer.writerow(header_row)
            # write remaining CSV data
            for (row_title, summary_field) in [('p=none', 'dmarc_policy_none'), ('p=quarantine', 'dmarc_policy_quarantine'), ('policy_of_reject', 'dmarc_policy_reject'), ('reports_dmarc_to_cisa', 'dmarc_correct_rua'), ('invalid_dmarc_record', 'invalid_dmarc_record'), ('no_dmarc_record', 'no_dmarc_record'), ('domains_tested', 'base_domain_count')]:
                data_row = [row_title]
                for summary_item in trustymail_dmarc_summary:
                    data_row.append(summary_item[summary_field])
                data_writer.writerow(data_row)

    def __generate_bod_results_by_agency_attachment(self):
        header_fields = ('acronym', 'name', 'cfo_act',
                         'active_critical_vulns',
                         'overdue_critical_vulns_{}+_days'.format(
                            DAYS_UNTIL_OVERDUE_CRITICAL),
                         'active_high_vulns',
                         'overdue_high_vulns_{}+_days'.format(
                            DAYS_UNTIL_OVERDUE_HIGH),
                         'bod_18-01_web_compliant_%',
                         'bod_18-01_email_compliant_%',
                         '3des_exception'
        )
        data_fields = ('acronym', 'name', 'cfo_act_org',
                       'open_criticals',
                       'open_overdue_criticals',
                       'open_highs',
                       'open_overdue_highs',
                       'live_bod1801_web_compliant_pct',
                       'live_bod1801_email_compliant_pct',
                       '3des_exception'
        )

        with open(BOD_RESULTS_BY_AGENCY_CSV_FILE, 'wb') as out_file:
            header_writer = csv.DictWriter(out_file, header_fields,
                                           extrasaction='ignore', quoting=csv.QUOTE_MINIMAL)
            data_writer = csv.DictWriter(out_file, data_fields,
                                         extrasaction='ignore', quoting=csv.QUOTE_MINIMAL)
            header_writer.writeheader()
            for org in copy.deepcopy(self.__scorecard_doc['scores']):
                if org['vuln-scan']['scanned']:
                    for vuln_scan_key in ('open_criticals',
                                          'open_overdue_criticals',
                                          'open_highs',
                                          'open_overdue_highs'):
                        org[vuln_scan_key] = org['vuln-scan']['metrics'].get(
                                                                vuln_scan_key)
                else:
                    org['open_criticals'] = 'Not vuln-scanned by CyHy'
                    for vuln_scan_key in ('open_overdue_criticals',
                                          'open_highs',
                                          'open_overdue_highs'):
                        org[vuln_scan_key] = 'N/A'

                if org['https-scan']['scanned']:
                    org['live_bod1801_web_compliant_pct'] = org[
                        'https-scan'][
                        'live_domains'].get(
                        'live_bod1801_web_compliant_pct')
                else:
                    org['live_bod1801_web_compliant_pct'] = \
                        'No known HTTP-responsive hosts'

                if org['trustymail']['scanned']:
                    org['live_bod1801_email_compliant_pct'] = org[
                        'trustymail'][
                        'base_domains_and_smtp_subdomains'].get(
                        'live_bod1801_email_compliant_pct')
                else:
                    org['live_bod1801_email_compliant_pct'] = \
                        'No known live domains or SMTP-responsive subdomains'

                data_writer.writerow(org)

    def __generate_bod1501_results_by_agency_attachment(self):
        prev_scorecard_date_txt = parser.parse(
                                    self.__previous_scorecard_data[
                                      'generated_time']).strftime('%Y-%m-%d')
        header_fields = ('acronym', 'name', 'cfo_act',
                         'active_critical_vulns',
                         'delta_active_critical_vulns_since_' +
                         prev_scorecard_date_txt,
                         'active_critical_vulns_0-7_days',
                         'active_critical_vulns_7-15_days',
                         'active_critical_vulns_15-30_days',
                         'active_critical_vulns_30-90_days',
                         'active_critical_vulns_90+_days',
                         'active_high_vulns',
                         'delta_active_high_vulns_since_' +
                         prev_scorecard_date_txt,
                         'active_high_vulns_0-7_days',
                         'active_high_vulns_7-15_days',
                         'active_high_vulns_15-30_days',
                         'active_high_vulns_30-90_days',
                         'active_high_vulns_90+_days')
        data_fields = ('acronym', 'name', 'cfo_act_org', 'open_criticals',
                       'open_criticals_delta_since_last_scorecard',
                       'open_criticals_0-7_days',
                       'open_criticals_7-15_days',
                       'open_criticals_15-30_days',
                       'open_criticals_30-90_days',
                       'open_criticals_more_than_90_days',
                       'open_highs',
                       'open_highs_delta_since_last_scorecard',
                       'open_highs_0-7_days',
                       'open_highs_7-15_days',
                       'open_highs_15-30_days',
                       'open_highs_30-90_days',
                       'open_highs_more_than_90_days')

        with open(BOD_1902_RESULTS_BY_AGENCY_CSV_FILE, 'wb') as out_file:
            header_writer = csv.DictWriter(out_file, header_fields,
                                           extrasaction='ignore', quoting=csv.QUOTE_MINIMAL)
            data_writer = csv.DictWriter(out_file, data_fields,
                                         extrasaction='ignore', quoting=csv.QUOTE_MINIMAL)
            header_writer.writeheader()
            for org in copy.deepcopy(self.__scorecard_doc['scores']):
                if org['vuln-scan']['scanned']:
                    for vuln_scan_key in (
                      'open_criticals',
                      'open_criticals_delta_since_last_scorecard',
                      'open_criticals_0-7_days',
                      'open_criticals_7-15_days',
                      'open_criticals_15-30_days',
                      'open_criticals_30-90_days',
                      'open_criticals_more_than_90_days',
                      'open_highs',
                      'open_highs_delta_since_last_scorecard',
                      'open_highs_0-7_days',
                      'open_highs_7-15_days',
                      'open_highs_15-30_days',
                      'open_highs_30-90_days',
                      'open_highs_more_than_90_days'):
                        org[vuln_scan_key] = org['vuln-scan']['metrics'].get(
                                                                vuln_scan_key)
                else:
                    org['open_criticals'] = 'Not vuln-scanned by CyHy'
                    for vuln_scan_key in (
                      'open_criticals_delta_since_last_scorecard',
                      'open_criticals_0-7_days',
                      'open_criticals_7-15_days',
                      'open_criticals_15-30_days',
                      'open_criticals_30-90_days',
                      'open_criticals_more_than_90_days',
                      'open_highs',
                      'open_highs_delta_since_last_scorecard',
                      'open_highs_0-7_days',
                      'open_highs_7-15_days',
                      'open_highs_15-30_days',
                      'open_highs_30-90_days',
                      'open_highs_more_than_90_days'):
                        org[vuln_scan_key] = 'N/A'
                data_writer.writerow(org)

    def __generate_web_security_results_by_agency_attachment(self):
        header_fields = ('acronym', 'name', 'cfo_act', 'http_responsive_hosts', 'uses_https', 'uses_https_%', 'enforces_https', 'enforces_https_%', 'uses_strong_hsts', 'uses_strong_hsts_%', 'free_of_sslv2/v3,3des,rc4', 'free_of_sslv2/v3,3des,rc4_%', 'bod_18-01_web_compliant', 'bod_18-01_web_compliant_%')
        data_fields = ('acronym', 'name', 'cfo_act_org', 'live_domain_count', 'live_supports_https_count', 'live_supports_https_pct', 'live_enforces_https_count', 'live_enforces_https_pct', 'live_uses_strong_hsts_count', 'live_uses_strong_hsts_pct', 'live_no_weak_crypto_count', 'live_no_weak_crypto_pct', 'live_bod1801_web_compliant_count', 'live_bod1801_web_compliant_pct')
        with open(WEB_SECURITY_RESULTS_BY_AGENCY_CSV_FILE, 'wb') as out_file:
            header_writer = csv.DictWriter(out_file, header_fields, extrasaction='ignore', quoting=csv.QUOTE_MINIMAL)
            data_writer = csv.DictWriter(out_file, data_fields, extrasaction='ignore', quoting=csv.QUOTE_MINIMAL)
            header_writer.writeheader()
            for org in copy.deepcopy(self.__scorecard_doc['scores']):
                for https_scan_key in ('live_domain_count', 'live_supports_https_count', 'live_supports_https_pct', 'live_enforces_https_count', 'live_enforces_https_pct', 'live_uses_strong_hsts_count', 'live_uses_strong_hsts_pct', 'live_no_weak_crypto_count', 'live_no_weak_crypto_pct', 'live_bod1801_web_compliant_count', 'live_bod1801_web_compliant_pct'):
                    if org['https-scan']['scanned']:
                        org[https_scan_key] = org['https-scan']['live_domains'].get(https_scan_key)
                    else:
                        org[https_scan_key] = 'N/A'
                data_writer.writerow(org)

    def __generate_email_security_results_by_agency_attachment(self):
        header_fields = ('acronym', 'name', 'cfo_act', 'live_domains_and_smtp_subdomains', 'valid_dmarc_record', 'valid_dmarc_record_%', 'dmarc_policy_of_reject', 'dmarc_policy_of_reject_%', 'reports_dmarc_to_cisa', 'reports_dmarc_to_cisa_%', 'supports_starttls', 'supports_starttls_%', 'has_spf_covered', 'has_spf_covered_%', 'free_of_sslv2/v3,3des,rc4', 'free_of_sslv2/v3,3des,rc4_%', 'bod_18-01_email_compliant', 'bod_18-01_email_compliant_%', '3des_exception')
        data_fields = ('acronym', 'name', 'cfo_act_org', 'live_domain_count', 'live_valid_dmarc_count', 'live_valid_dmarc_pct', 'live_dmarc_reject_count', 'live_dmarc_reject_pct', 'live_has_bod1801_dmarc_uri_count', 'live_has_bod1801_dmarc_uri_pct', 'live_supports_starttls_count', 'live_supports_starttls_pct', 'live_spf_covered_count', 'live_spf_covered_pct', 'live_no_weak_crypto_count', 'live_no_weak_crypto_pct', 'live_bod1801_email_compliant_count', 'live_bod1801_email_compliant_pct', '3des_exception')
        with open(EMAIL_SECURITY_RESULTS_BY_AGENCY_CSV_FILE, 'wb') as out_file:
            header_writer = csv.DictWriter(out_file, header_fields, extrasaction='ignore', quoting=csv.QUOTE_MINIMAL)
            data_writer = csv.DictWriter(out_file, data_fields, extrasaction='ignore', quoting=csv.QUOTE_MINIMAL)
            header_writer.writeheader()
            for org in copy.deepcopy(self.__scorecard_doc['scores']):
                for trustymail_key in ('live_domain_count', 'live_valid_dmarc_count', 'live_valid_dmarc_pct', 'live_dmarc_reject_count', 'live_dmarc_reject_pct', 'live_has_bod1801_dmarc_uri_count', 'live_has_bod1801_dmarc_uri_pct', 'live_supports_starttls_count', 'live_supports_starttls_pct', 'live_spf_covered_count', 'live_spf_covered_pct', 'live_no_weak_crypto_count', 'live_no_weak_crypto_pct', 'live_bod1801_email_compliant_count', 'live_bod1801_email_compliant_pct'):
                    if org['trustymail']['scanned']:
                        org[trustymail_key] = org['trustymail']['base_domains_and_smtp_subdomains'].get(trustymail_key)
                    else:
                        org[trustymail_key] = 'N/A'
                data_writer.writerow(org)

    ###############################################################################
    # Chart PDF Generation
    ###############################################################################
    def __figure_vuln_age_history_graph(self, ticket_age_dataframe, figure_filename, severity_label):
        if len(ticket_age_dataframe):
            line = graphs.MyStackedLine(ticket_age_dataframe, ylabel='{} Vulnerabilities'.format(severity_label), data_labels=['Active Less Than 30 Days', 'Active 30+ Days'], data_fill_colors=['#0099cc', '#cc0000'])
            line.plot(figure_filename, size=1.6)
        else:
            message = graphs.MyMessage('No {} Vulnerabilities To Display\nFigure Omitted'.format(severity_label))
            message.plot(figure_filename, size=1.0)

    def __figure_vuln_age_distribution(self, tickets, figure_filename, severity_label, max_age_cutoff, ticket_age_buckets):
        age_buckets = list()
        for t in tickets:
            days_open = (self.__generated_time - t['time_opened']).days
            if days_open >= max_age_cutoff:
                age_buckets.append(max_age_cutoff)
            else:
                age_buckets.append(days_open)
        if len(age_buckets):
            age_buckets.sort()
            s1 = Series(age_buckets)
            s2 = s1.value_counts().reindex(range(max_age_cutoff+1)).fillna(0)
            region_colors = [(ticket_age_buckets[0][1],'#ffffb2'), (ticket_age_buckets[1][1],'#fecc5c'), (ticket_age_buckets[2][1],'#fd8d3c'), (ticket_age_buckets[3][1],'#f03b20'), (ticket_age_buckets[4][1],'#bd0026')] # Colorize regions
            bar = graphs.MyDistributionBar(s2, xlabel='Age (Days)', ylabel='{} Vulnerabilities'.format(severity_label), final_bucket_accumulate=True, x_major_tick_count=10, region_colors=region_colors, x_limit_extra=2)
            bar.plot(figure_filename, size=1.6)
            #self.__results['active_critical_age_counts'] = s2
        else:
            message = graphs.MyMessage('No {} Vulnerabilities To Display\nFigure Omitted'.format(severity_label))
            message.plot(figure_filename, size=1.0)
            #self.__results['active_critical_age_counts'] = Series().reindex(range(max_age_cutoff+1)).fillna(0)

    def __figure_bod1801_email_components(self):
        import matplotlib.pyplot as plt
        # Override default figsize
        fig_width = 10.0
        fig_height = 4.0
        plt.rcParams.update({'figure.figsize':[fig_width, fig_height]})

        bod_1801_email_bar = graphs.MyTrustyBar(
            percentage_list=[self.__results['federal_totals']['trustymail']['base_domains_and_smtp_subdomains']['live_valid_dmarc_pct_int'],
                             self.__results['federal_totals']['trustymail']['base_domains_and_smtp_subdomains']['live_dmarc_reject_pct_int'],
                             self.__results['federal_totals']['trustymail']['base_domains_and_smtp_subdomains']['live_has_bod1801_dmarc_uri_pct_int'],
                             self.__results['federal_totals']['trustymail']['base_domains_and_smtp_subdomains']['live_supports_starttls_pct_int'],
                             self.__results['federal_totals']['trustymail']['base_domains_and_smtp_subdomains']['live_spf_covered_pct_int'],
                             self.__results['federal_totals']['trustymail']['base_domains_and_smtp_subdomains']['live_no_weak_crypto_pct_int']],
            label_list=['Valid\nDMARC', 'DMARC\nPolicy of Reject', 'Reports DMARC\nto CISA', 'Supports\nSTARTTLS', 'SPF\nCovered', 'No SSLv2/v3,\n3DES,RC4'],
            fill_color=graphs.DARK_BLUE,
            title='BOD 18-01 Email Components')
        bod_1801_email_bar.plot(filename='figure_bod1801_email_components')

    def __figure_bod1801_email_compliant(self):
        donut = graphs.MyDonutPie(
            percentage_full=self.__results['federal_totals']['trustymail']['base_domains_and_smtp_subdomains']['live_bod1801_email_compliant_pct_int'],
            label='BOD 18-01\nCompliant\n(Email)',
            fill_color=graphs.DARK_BLUE)
        donut.plot(filename='figure_bod1801_email_compliant')

    def __figure_bod1801_web_components(self):
        import matplotlib.pyplot as plt
        # Override default figsize
        fig_width = 6.0
        fig_height = 4.0
        plt.rcParams.update({'figure.figsize':[fig_width, fig_height]})

        bod_1801_web_bar = graphs.MyTrustyBar(percentage_list=[self.__results['federal_totals']['https-scan']['live_domains']['live_supports_https_pct_int'],
                                            self.__results['federal_totals']['https-scan']['live_domains']['live_enforces_https_pct_int'],
                                            self.__results['federal_totals']['https-scan']['live_domains']['live_uses_strong_hsts_pct_int'],
                                            self.__results['federal_totals']['https-scan']['live_domains']['live_no_weak_crypto_pct_int']],
                           label_list=['Uses\nHTTPS', 'Enforces\nHTTPS', 'Uses Strong\nHSTS', 'No SSLv2/v3,\n3DES,RC4'],
                           fill_color=graphs.DARK_BLUE,
                           title='BOD 18-01 Web Components')
        bod_1801_web_bar.plot(filename='figure_bod1801_web_components')

    def __figure_bod1801_web_compliant(self):
        donut = graphs.MyDonutPie(percentage_full=self.__results['federal_totals']['https-scan']['live_domains']['live_bod1801_web_compliant_pct_int'],
                                  label='BOD 18-01\nCompliant\n(Web)',
                                  fill_color=graphs.DARK_BLUE)
        donut.plot(filename='figure_bod1801_web_compliant')

    def __generate_charts(self):
        graphs.setup()

        if self.__debug:
            output = sys.stdout
        else:
            output = open(os.devnull, 'w')

        # trustymail charts
        self.__figure_bod1801_email_components()
        self.__figure_bod1801_email_compliant()

        # https-scan charts
        self.__figure_bod1801_web_components()
        self.__figure_bod1801_web_compliant()

    ###############################################################################
    # Final Document Generation and Assembly
    ###############################################################################
    def __generate_mustache_json(self, filename):
        result = {'all_orgs_alpha':self.__scorecard_doc['scores']}
        result['orgs_with_recently_issued_certs'] = self.__orgs_with_recently_issued_certs
        result['orgs_with_no_recently_issued_certs'] = self.__orgs_with_no_recently_issued_certs
        result['orgs_with_no_known_domains'] = self.__orgs_with_no_known_domains
        result['all_orgs_ed1901_cert'] = sorted(self.__scorecard_doc['scores'], key=lambda x:(x['cert-scan']['metrics'].get('certs_issued_past_7_days_count'), x['cert-scan']['metrics'].get('certs_issued_past_30_days_count'), x['cert-scan']['metrics'].get('certs_issued_current_fy_count'), x['cert-scan']['metrics'].get('unexpired_certs_count'), x['cert-scan'].get('scanned')), reverse=True)
        result['orgs_with_criticals'] = self.__orgs_with_criticals
        result['orgs_with_highs'] = self.__orgs_with_highs
        result['orgs_without_criticals_or_highs'] = self.__orgs_without_criticals_or_highs
        result['orgs_not_vuln_scanned'] = self.__orgs_not_vuln_scanned
        result['overall_bod_orgs'] = sorted(self.__scorecard_doc['scores'],
                                            key=lambda x: (
            x['vuln-scan']['metrics'].get('open_overdue_criticals'),
            x['vuln-scan']['metrics'].get('open_criticals'),
            x['vuln-scan']['metrics'].get('open_overdue_highs'),
            x['vuln-scan']['metrics'].get('open_highs'),
            x['https-scan']['live_domains'].get(
                'live_bod1801_web_compliant_pct'),
            x['trustymail']['base_domains_and_smtp_subdomains'].get(
                'live_bod1801_email_compliant_pct')), reverse=True)
        result['bod_1902_orgs'] = sorted(self.__scorecard_doc['scores'],
                                         key=lambda x: (
            x['vuln-scan']['metrics'].get('open_criticals'),
            x['vuln-scan']['metrics'].get('open_highs'),
            x['vuln-scan']['metrics'].get('open_criticals_more_than_90_days'),
            x['vuln-scan']['metrics'].get('open_highs_more_than_90_days'),
            x['vuln-scan']['metrics'].get('open_criticals_30-90_days'),
            x['vuln-scan']['metrics'].get('open_highs_30-90_days'),
            x['vuln-scan']['metrics'].get('open_criticals_15-30_days'),
            x['vuln-scan']['metrics'].get('open_highs_15-30_days'),
            x['vuln-scan']['metrics'].get('open_criticals_7-15_days'),
            x['vuln-scan']['metrics'].get('open_highs_7-15_days'),
            x['vuln-scan'].get('scanned')),
            reverse=True)
        result['strong_hsts_all'] = self.__strong_hsts_all
        result['strong_hsts_some'] = self.__strong_hsts_some
        result['strong_hsts_none'] = self.__strong_hsts_none
        result['dmarc_reject_all'] = self.__dmarc_reject_all
        result['dmarc_reject_some'] = self.__dmarc_reject_some
        result['dmarc_reject_none'] = self.__dmarc_reject_none

        result['all_orgs_bod1801_email_compliant'] = sorted(self.__scorecard_doc['scores'], key=lambda x:(x['trustymail']['base_domains_and_smtp_subdomains'].get('live_bod1801_email_compliant_pct'), x['trustymail']['base_domains_and_smtp_subdomains'].get('live_dmarc_reject_pct'), x['trustymail']['base_domains_and_smtp_subdomains'].get('live_valid_dmarc_pct'),
        x['trustymail']['base_domains_and_smtp_subdomains'].get('live_has_bod1801_dmarc_uri_pct'), x['trustymail']['base_domains_and_smtp_subdomains'].get('live_spf_covered_pct'),
        x['trustymail']['base_domains_and_smtp_subdomains'].get('live_supports_starttls_pct'),
        x['trustymail']['base_domains_and_smtp_subdomains'].get('live_no_weak_crypto_pct'),
        x['trustymail']['base_domains_and_smtp_subdomains'].get('live_domain_count')), reverse=True)

        # Reverse the sort from the query for display purposes
        result['dmarc_summary'] = sorted(self.__results['trustymail_dmarc_summary'], key=lambda x:x['_id'])
        for dmarc_summary_item in result['dmarc_summary']:
                dmarc_summary_item['scan_date_tex'] = dmarc_summary_item['_id'].strftime('{%d}{%m}{%Y}')

        result['all_orgs_bod1801_web_compliant'] = sorted(self.__scorecard_doc['scores'], key=lambda x:(x['https-scan']['live_domains'].get('live_bod1801_web_compliant_pct'), x['https-scan']['live_domains'].get('live_uses_strong_hsts_pct'), x['https-scan']['live_domains'].get('live_enforces_https_pct'), x['https-scan']['live_domains'].get('live_supports_https_pct'), x['https-scan']['live_domains'].get('live_no_weak_crypto_pct'), x['https-scan']['live_domains'].get('live_domain_count')), reverse=True)

        result['currently_scanned_days'] = CURRENTLY_SCANNED_DAYS
        result['title_date_tex'] = self.__generated_time.strftime('{%d}{%m}{%Y}')
        result['draft'] = self.__draft
        result['federal_totals'] = self.__results['federal_totals']
        result['cfo_totals'] = self.__results['cfo_totals']
        result['non_cfo_totals'] = self.__results['non_cfo_totals']
        result['generated_time'] = self.__generated_time
        result['previous_scorecard_date_tex'] = parser.parse(self.__previous_scorecard_data['generated_time']).strftime('{%d}{%m}{%Y}')
        result['scorecard_name'] = self.__results['scorecard_name']
        # result['scorecard_subset_name'] = self.__results['scorecard_subset_name']
        result['days_until_criticals_overdue'] = DAYS_UNTIL_OVERDUE_CRITICAL
        result['days_until_highs_overdue'] = DAYS_UNTIL_OVERDUE_HIGH

        # Calculate earliest scan date for all scans (cyhy, trustymail, https-scan)
        # Since cyhy is always scanning, self.__generated_time will be used for 'latest scan date'
        CYHY_HOST_MAX_RESCAN_FREQUENCY_DAYS = scheduler.DefaultScheduler.PRIORITY_TIMES[-1]
        # Every cyhy host should've been scanned in the past CYHY_HOST_MAX_RESCAN_FREQUENCY_DAYS days
        earliest_scan_date = self.__generated_time - CYHY_HOST_MAX_RESCAN_FREQUENCY_DAYS
        for x in self.__results['trustymail_base_domains'] + self.__results['https-scan']:
            if x['earliest_scan_date'] < earliest_scan_date:
                earliest_scan_date = x['earliest_scan_date']
        result['earliest_scan_date_tex'] = earliest_scan_date.strftime('{%d}{%m}{%Y}')

        if self.__log_scorecard_to_db:
            result['scorecard_oid'] = str(self.__scorecard_oid)
        else:
            result['scorecard_oid'] = None      # If scorecard_oid is None, it will not be included in the PDF metadata

        # escape latex special characters in key lists
        for x in ('all_orgs_alpha', 'federal_totals', 'cfo_totals', 'non_cfo_totals'):
            self.__latex_escape_structure(result[x])

        with open(filename, 'wb') as out:
            out.write(to_json(result))

    def __generate_latex(self, mustache_file, json_file, latex_file):
        renderer = pystache.Renderer()
        template = codecs.open(mustache_file,'r', encoding='utf-8').read()

        with codecs.open(json_file,'r', encoding='utf-8') as data_file:
            data = json.load(data_file)

        r = pystache.render(template, data)
        with codecs.open(latex_file,'w', encoding='utf-8') as output:
            output.write(r)

    def __generate_final_pdf(self):
        if self.__debug:
            output = sys.stdout
        else:
            output = open(os.devnull, 'w')

        return_code = subprocess.call(['xelatex', REPORT_TEX], stdout=output, stderr=subprocess.STDOUT)
        assert return_code == 0, 'xelatex pass 1 of 2 return code was %s' % return_code

        return_code = subprocess.call(['xelatex', REPORT_TEX], stdout=output, stderr=subprocess.STDOUT)
        assert return_code == 0, 'xelatex pass 2 of 2 return code was %s' % return_code

    def __log_scorecard_report(self):
        report = self.__cyhy_db.ReportDoc()
        report['_id'] = self.__scorecard_oid
        report['generated_time'] = self.__generated_time
        report['report_types'] = [REPORT_TYPE.CYBEX]
        report.save()

def generate_empty_scorecard_json():
    current_time = utcnow()
    result = { 'orgs_with_recently_issued_certs': [] }
    result['orgs_with_no_recently_issued_certs'] = []
    result['orgs_with_no_known_domains'] = []
    result['orgs_with_criticals'] = []
    result['orgs_without_criticals'] = []
    result['orgs_with_highs'] = []
    result['orgs_without_highs'] = []
    result['orgs_not_vuln_scanned'] = []
    result['all_orgs_alpha'] = []
    result['all_orgs_bod1801_email_compliant'] = []
    result['all_orgs_bod1801_web_compliant'] = []
    result['overall_bod_orgs'] = []
    result['bod_1902_orgs'] = []
    result['strong_hsts_all'] = []
    result['strong_hsts_some'] = []
    result['strong_hsts_none'] = []
    result['dmarc_reject_all'] = []
    result['dmarc_reject_some'] = []
    result['dmarc_reject_none'] = []
    result['currently_scanned_days'] = CURRENTLY_SCANNED_DAYS
    result['title_date_tex'] = current_time.strftime('{%d}{%m}{%Y}')
    result['draft'] = True
    empty_totals = {'vuln-scan':
                    {'metrics':
                     {'open_criticals_on_previous_scorecard': 0,
                      'open_highs_on_previous_scorecard': 0}},
                    'trustymail': {'base_domains': {},
                                   'base_domains_and_smtp_subdomains': {}},
                    'https-scan': {'live_domains': {}},
                    'sslyze-scan': {'live_domains': {}}}
    result['federal_totals'] = empty_totals
    result['cfo_totals'] = empty_totals
    result['non_cfo_totals'] = empty_totals
    result['generated_time'] = current_time
    result['previous_scorecard_date_tex'] = current_time.strftime('{%d}{%m}{%Y}')
    result['scorecard_oid'] = None
    result['scorecard_name'] = ""
    # result['scorecard_subset_name'] = ""
    return to_json(result)

def main():
    args = docopt(__doc__, version='v0.0.1')

    if args['--generate-empty-scorecard-json']:
        print generate_empty_scorecard_json()
        sys.exit(0)

    cyhy_db = database.db_from_config(args['CYHY_DB_SECTION'])
    scan_db = database.db_from_config(args['SCAN_DB_SECTION'])

    # Grab OCSP/CRL hosts.  These hosts are to be removed from the
    # list of hosts to be evaluated for HTTPS compliance, since they
    # are not required to satisfy BOD 18-01.  For more information see
    # here:
    # https://https.cio.gov/guide/#are-federally-operated-certificate-revocation-services-crl-ocsp-also-required-to-move-to-https
    response = requests.get(OCSP_URL)
    with open(OCSP_FILE, 'w') as f:
        f.write(response.text)

    # Grab a CSV file listing whether or not organizations have been
    # issued a 3DES exception by CISA.  Such organizations are adorned
    # with an asterisk in the scorecard.
    response = requests.get(TRIPLE_DES_EXCEPTIONS_URL)
    with open(TRIPLE_DES_EXCEPTIONS_FILE, 'w') as f:
        f.write(response.text)

    if cyhy_db.RequestDoc.find_one({'report_types':REPORT_TYPE.CYBEX}):
        print 'Generating Cyber Exposure Scorecard...'
        generator = ScorecardGenerator(cyhy_db, scan_db, OCSP_FILE,
                                       TRIPLE_DES_EXCEPTIONS_FILE,
                                       args['PREVIOUS_SCORECARD_JSON_FILE'],
                                       debug=args['--debug'],
                                       final=args['--final'],
                                       log_scorecard=not args['--nolog'],
                                       anonymize=args['--anonymize'])
        results = generator.generate_cybex_scorecard()
        print 'Done'
    else:
        print 'No organizations found in database with "{}" report type - exiting!'.format(REPORT_TYPE.CYBEX)
    sys.exit(0)

    # import IPython; IPython.embed() #<<< BREAKPOINT >>>
    # sys.exit(0)

if __name__=='__main__':
    main()
