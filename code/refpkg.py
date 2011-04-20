import itertools
import hashlib
import os.path
import sqlite3
import json
import csv

class VerificationFailed(Exception):
    pass

class NoAncestor(Exception):
    pass

class OnUpdate(object):
    def __init__(self, proxied):
        self.proxied = proxied
        self.setter = None

    def __get__(self, inst, cls):
        if inst is None:
            return self
        return getattr(inst, self.proxied)

    def __set__(self, inst, value):
        if self.setter:
            self.setter(inst, value)
        setattr(inst, self.proxied, value)

    def __call__(self, f):
        self.setter = f
        return self

class _IntermediateTaxon(object):
    def __init__(self, tax_id, parent, rank, tax_name):
        self.children = set()
        self.tax_id = tax_id
        self.parent = parent
        self.rank = rank
        self.tax_name = tax_name

    _parent = _adjacent_to = None

    @OnUpdate('_parent')
    def parent(self, p):
        if self.parent is not None:
            self.parent.children.discard(self)
        if p is not None:
            p.children.add(self)

    @OnUpdate('_adjacent_to')
    def adjacent_to(self, n):
        if n is not None:
            self.parent = n.parent

    def iterate_children(self, on_pop=None, including_self=True):
        search_stack = [(None, set([self]))]
        while search_stack:
            if not search_stack[-1][1]:
                parent, _ = search_stack.pop()
                if on_pop:
                    on_pop(parent)
                continue
            node = search_stack[-1][1].pop()
            if node is not self or including_self:
                yield node
            search_stack.append((node, node.children.copy()))

class Refpkg(object):
    def __init__(self, path):
        self.path = path
        self.contents = json.load(self.file_resource('CONTENTS.json', 'rU'))
        self.db = None

    file_factory = open
    def file_resource(self, resource, *mode):
        return self.file_factory(os.path.join(self.path, resource), *mode)

    def resource(self, name, *mode):
        return self.file_resource(self.contents['files'][name], *mode)

    def verify(self, name):
        md5 = hashlib.md5()
        fobj = self.resource(name, 'rb')
        for block in iter(lambda: fobj.read(4096), ''):
            md5.update(block)
        if md5.hexdigest() != self.contents['md5'][name]:
            raise VerificationFailed(name)

    def load_db(self):
        db = sqlite3.connect(':memory:')
        curs = db.cursor()

        curs.execute("""
            CREATE TABLE ranks (
              rank TEXT PRIMARY KEY NOT NULL,
              rank_order INT
            )
        """)

        curs.execute("""
            CREATE TABLE taxa (
              tax_id TEXT PRIMARY KEY NOT NULL,
              tax_name TEXT NOT NULL,
              rank TEXT REFERENCES ranks (rank) NOT NULL
            )
        """)

        curs.execute("""
            CREATE TABLE sequences (
              seqname TEXT PRIMARY KEY NOT NULL,
              tax_id TEXT REFERENCES taxa (tax_id) NOT NULL
            )
        """)

        curs.execute("""
            CREATE TABLE hierarchy (
              tax_id TEXT REFERENCES taxa (tax_id) PRIMARY KEY NOT NULL,
              lft INT NOT NULL,
              rgt INT NOT NULL
            )
        """)

        curs.execute("""
            CREATE VIEW parents AS
            SELECT h1.tax_id AS child,
                   h2.tax_id AS parent
            FROM   hierarchy h1
                   JOIN hierarchy h2
                     ON h1.lft BETWEEN h2.lft AND h2.rgt
        """)

        taxon_map = {}
        reader = csv.DictReader(self.resource('taxonomy', 'rU'))
        for row in reader:
            parent = taxon_map.get(row['parent_id'])
            taxon = _IntermediateTaxon(
                row['tax_id'], parent, row['rank'], row['tax_name'])
            taxon_map[taxon.tax_id] = taxon

        counter = itertools.count(1).next
        def on_pop(parent):
            if parent is not None:
                parent.rgt = counter()
        for node in taxon_map['1'].iterate_children(on_pop=on_pop):
            node.lft = counter()

        curs.executemany("INSERT INTO ranks (rank_order, rank) VALUES (?, ?)",
            enumerate(reader._fieldnames[4:]))
        curs.executemany("INSERT INTO taxa VALUES (?, ?, ?)",
            ((t.tax_id, t.tax_name, t.rank) for t in taxon_map.itervalues()))
        curs.executemany("INSERT INTO hierarchy VALUES (?, ?, ?)",
            ((t.tax_id, t.lft, t.rgt) for t in taxon_map.itervalues()))

        reader = csv.DictReader(self.resource('seq_info', 'rU'))
        curs.executemany("INSERT INTO sequences VALUES (?, ?)",
            ((row['seqname'], row['tax_id']) for row in reader))

        db.commit()
        self.db = db

    def most_recent_common_ancestor(self, t1, t2):
        cursor = self.db.cursor()
        cursor.execute("""
            SELECT t3.tax_id
            FROM   taxa t1
                   JOIN parents p1
                     ON t1.tax_id = p1.child
                   JOIN parents p2 USING (parent)
                   JOIN taxa t2
                     ON t2.tax_id = p2.child
                   JOIN taxa t3
                     ON t3.tax_id = parent
                   JOIN ranks r
                     ON t3.rank = r.rank
            WHERE  t1.tax_id = ?
                   AND t2.tax_id = ?
            ORDER  BY rank_order DESC
            LIMIT  1
        """, (t1, t2))

        res = cursor.fetchall()
        if res:
            (res,), = res
        else:
            raise NoAncestor()
        return res

if __name__ == '__main__':
    import sys
    rp = Refpkg(sys.argv[1])
