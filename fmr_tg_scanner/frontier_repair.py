"""Retire invalid historical channel guesses without deleting crawl checkpoints."""
import json
import re


def repair(store):
    changed=0
    for row in store.query("SELECT key,payload FROM tg_search_tasks WHERE kind IN ('admit','query','head','history','post') AND status!='invalid'"):
        channel=json.loads(row['payload']).get('channel','')
        if not re.fullmatch('[a-z][a-z0-9_]{3,31}',channel):
            store.execute("UPDATE tg_search_tasks SET status='invalid',last_error='InvalidTelegramUsername' WHERE key=?",(row['key'],))
            changed+=1
    return changed
