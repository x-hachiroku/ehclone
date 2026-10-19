import json
import os
import sys
import tempfile
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
test_appdata = tempfile.TemporaryDirectory(prefix='ehclone-tests-')
os.environ['APPDATA'] = test_appdata.name
Path(test_appdata.name, 'config.json').write_text(json.dumps({
    # Explicit test inputs keep behavior tests independent of product defaults.
    'db': {'url': 'postgresql+psycopg2://localhost/ehclone_test'},
    'filter': {
        'min_rating': 0, 'not_before_min': 60, 'include_categories': [], 'include_tags': [],
        'exclude_tags': [],
        'dedupe': {
            'thumb_dir': test_appdata.name, 'enabled': False,
            'candidate_limit': 16, 'page_tolerance': 8, 'distance_threshold': 0.05,
            'page_tolerance_rate': 0.025, 'page_tolerance_ad': 8,
            'rating_factor': 20, 'page_count_factor': 5,
            'tag_biases': {}, 'expunged_tags': [], 'expunged_bias': -100,
        },
    },
    'aria2': {'url': 'http://aria2/jsonrpc', 'token': None,
              'remote_dir': '/remote/aria2', 'local_dir': '/local/aria2', 'task_limit': 8},
    'qbit': {'url': 'http://qbit', 'username': None, 'password': None,
             'remote_dir': '/remote/qbit', 'local_dir': '/local/qbit',
             'enabled': True, 'task_limit': 8, 'timeout_hours': 168},
    'log': {'dir': None},
    'eh': {'min_request_interval': 0, 'recheck': 0, 'include_expunged': False},
}))
