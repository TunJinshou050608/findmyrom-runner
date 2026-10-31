"""Stdlib-only independent storage; interoperable with the project's D1 client API."""
import sqlite3


class SQLiteDB:
    def __init__(self,path):
        self.connection=sqlite3.connect(str(path),isolation_level=None)
        self.connection.row_factory=sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=ON')
    def query(self,sql,params=()):return [dict(row) for row in self.connection.execute(sql,params)]
    def execute(self,sql,params=(),**_kwargs):
        cursor=self.connection.execute(sql,params)
        return {'changes':max(cursor.rowcount,0),'last_row_id':cursor.lastrowid,'success':True}
    def execute_script(self,script):self.connection.executescript(script)
    def execute_batch(self,statements):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            changes=0
            for sql,params in statements:changes+=self.execute(sql,params)['changes']
            self.connection.execute('COMMIT');return {'changes':changes,'success':True}
        except BaseException:
            self.connection.execute('ROLLBACK');raise
    def close(self):self.connection.close()
