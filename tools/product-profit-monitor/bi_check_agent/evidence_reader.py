"""Bounded read-only evidence queries, with an aggregate budget per investigation."""
import os
from bi_check_agent import db


class Reader:
    def __init__(self, query_fn=None):
        self.query = query_fn or db.run_query
        self.rows = self.bytes = 0

    def read(self, sql, params):
        cap = int(os.getenv('MAX_RESULT_ROWS', '50000'))
        if cap < 1:
            raise ValueError('证据读取上限必须大于零')
        with db.source_timezone():
            frame = self.query(sql + '\nLIMIT :evidence_limit', {**params, 'evidence_limit': cap + 1})
        self.rows += len(frame)
        self.bytes += int(frame.memory_usage(index=True, deep=True).sum())
        if (len(frame) > cap or self.rows > int(os.getenv('MAX_QUERY_TOTAL_ROWS', '500000'))
                or self.bytes > int(os.getenv('MAX_QUERY_MEMORY_MB', '256')) * 1024 * 1024):
            raise RuntimeError('证据读取超过上限，保留疑点')
        return frame.to_dict('records')


def bindings(values, prefix='e'):
    params = {f'{prefix}{i}': int(v) for i, v in enumerate(values)}
    return params, ','.join(':' + key for key in params)
