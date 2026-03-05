import inject
from flask_pymongo import PyMongo
from flask import g

from cmr_etl_lib.utils import generate_id


class Document:
    __TABLE__ = None
    _id = None

    def __init__(self, **kwargs):
        g.table_name = self.__TABLE__
        for k, v in kwargs.items():
            self.__setattr__(k, v)

    @property
    def id(self):
        return self._id

    @id.setter
    def id(self, value):
        self._id = value

    @classmethod
    def get_collection(cls, collection_name):
        mongo = inject.instance(PyMongo)
        return mongo.db[collection_name]

    def db(self):
        return self.get_collection(self.__TABLE__)

    def save(self):
        if not self._id:
            self._id = generate_id()
        self._id = self.db().save(self.to_dict())
        return self

    def save_all(self, items, **kwargs):
        kwargs = kwargs or {}
        items = [{"_id": generate_id(), **item, **kwargs} for item in items]
        self.db().insert_many(items)
        return items

    def load(self, query=None):
        if not query:
            query = {"_id": self._id}
        self.from_dict(self.db().find_one(query))
        return self

    def delete(self, query=None):
        if self._id:
            if not query:
                query = {"_id": self._id}
            self.db().remove(query)
        return self

    def to_dict(self):
        return self.__dict__

    def from_dict(self, d):
        if d:
            self.__dict__ = d
        else:
            self._id = None
        return self

    @classmethod
    def get_all(cls, query=None):
        if query is None:
            query = {}
        return [cls(**r) for r in cls().db().find(query)]

    @classmethod
    def drop(cls):
        return cls().db().drop()

    @classmethod
    def delete_all(cls, query):
        if query:
            cls().db().delete_many(query)

    def update(self, data: dict):
        self.db().update_one({"_id": self._id}, {"$set": data})
        # for k, v in data.items():
        #     self.__setattr__(k, v)
        # return self
        
    @classmethod
    def count(cls, query=None):
        """
        Count documents matching the query.
        
        :param query: dict, The filter query (default: {}).
        :return: int, Number of matching documents.
        """
        query = query or {}
        return cls().db().count_documents(query)
