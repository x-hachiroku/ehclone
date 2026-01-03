import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Integer,
    String,
    Table,
    Column,
    Index,
    ForeignKey,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship, declarative_base
from pgvector.sqlalchemy import Vector


Base = declarative_base()


gallery_tag = Table(
    'gallery_tag',
    Base.metadata,
    Column('gallery_gid', BigInteger, ForeignKey('gallery.gid', ondelete='CASCADE'), primary_key=True),
    Column('tag_id', Integer, ForeignKey('tag.id'), primary_key=True)
)


class Category(str, enum.Enum):
    DOUJINSHI = 'Doujinshi'
    MANGA = 'Manga'
    ARTIST_CG = 'Artist CG'
    GAME_CG = 'Game CG'
    WESTERN = 'Western'
    IMAGE_SET = 'Image Set'
    NON_H = 'Non-H'
    COSPLAY = 'Cosplay'
    ASIAN_PORN = 'Asian Porn'
    MISC = 'Misc'


class Gallery(Base):
    __tablename__ = 'gallery'

    gid = Column(BigInteger, primary_key=True, autoincrement=False)
    token = Column(String, nullable=False)
    title = Column(String)
    title_jpn = Column(String)
    category = Column(Enum(Category))
    thumb_url = Column(String, ForeignKey('thumb.url'))
    uploader = Column(String)
    posted_at = Column(DateTime(timezone=False))
    filecount = Column(Integer)
    filesize = Column(BigInteger)
    expunged = Column(Boolean)
    rating = Column(Integer)

    first_gid = Column(BigInteger, ForeignKey('gallery.gid'))
    dupe_with = Column(BigInteger, ForeignKey('gallery.gid'), index=True)

    updated_at = Column(DateTime(timezone=False), server_default=func.now(), onupdate=func.now())

    downloaded = Column(Integer)

    tags = relationship('Tag', secondary=gallery_tag, back_populates='galleries', passive_deletes=True)
    thumb = relationship('Thumb', back_populates='galleries')
    torrents = relationship('Torrent', back_populates='gallery', passive_deletes=True)

    __table_args__ = (
        Index('ix_gallery_first_gid_gid', 'first_gid', 'gid'),
    )


class Thumb(Base):
    __tablename__ = 'thumb'

    url = Column(String, primary_key=True)
    mobile_net_v3 = Column(Vector(576))

    galleries = relationship('Gallery', back_populates='thumb')


class Tag(Base):
    __tablename__ = 'tag'

    id = Column(Integer, primary_key=True, autoincrement=True)
    namespace = Column(String, nullable=False)
    name = Column(String, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint('namespace', 'name', name='uq_tag_namespace_name'),
        Index('ix_tag_namespace_name', 'namespace', 'name'),
    )

    galleries = relationship('Gallery', secondary=gallery_tag, back_populates='tags')

    @classmethod
    def from_str(cls, tag_str):
        if ':' in tag_str:
            namespace, name = tag_str.split(':', 1)
        else:
            namespace = 'temp'
            name = tag_str
        return cls(namespace=namespace, name=name)


class Torrent(Base):
    __tablename__ = 'torrent'

    gid = Column(BigInteger, ForeignKey('gallery.gid', ondelete='CASCADE'), primary_key=True)
    infohash = Column(String, primary_key=True)
    added_at = Column(DateTime(timezone=False))
    name = Column(String)
    tsize = Column(BigInteger)
    fsize = Column(BigInteger)

    gallery = relationship('Gallery', back_populates='torrents')


class DownloadStatus(str, enum.Enum):
    QUEUED = 'queued'
    QBIT_DOWNLOADING = 'qbit_downloading'
    ARIA2_DOWNLOADING = 'aria2_downloading'
    QBIT_FAILED = 'qbit_failed'
    ARIA2_FAILED = 'aria2_failed'
    COMPLETED = 'completed'


class DownloadQueue(Base):
    __tablename__ = 'download_queue'

    gid = Column(BigInteger, ForeignKey('gallery.gid'), primary_key=True)
    status = Column(Enum(DownloadStatus))
    task_id = Column(String)
    started_at = Column(DateTime(timezone=False))
    updated_at = Column(DateTime(timezone=False))
