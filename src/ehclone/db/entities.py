import enum
from ehclone.config import Category as Category

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Integer,
    SmallInteger,
    String,
    Table,
    Column,
    Index,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship, declarative_base
from pgvector.sqlalchemy import Vector


Base = declarative_base()

# We shouldn't be deleting any record once inserted so no need to care about on delete / cascade.

gallery_tag = Table(
    'gallery_tag',
    Base.metadata,
    Column('gallery_gid', BigInteger, ForeignKey('gallery.gid'), primary_key=True),
    Column('tag_id', Integer, ForeignKey('tag.id'), primary_key=True),
    Index('ix_gallery_tag_tag_id_gallery_gid', 'tag_id', 'gallery_gid'),
)


class DownloadState(str, enum.Enum):
    QUEUED = 'queued'
    DOWNLOADING = 'downloading'
    DOWNLOAD_FAILED = 'download_failed'
    VALIDATION_FAILED = 'validation_failed'
    COMPLETED = 'completed'


class Gallery(Base):
    __tablename__ = 'gallery'

    # All timestamps are naive UTC, stored with timezone=False.
    gid = Column(BigInteger, primary_key=True, autoincrement=False)
    token = Column(String, nullable=False)
    title = Column(String)
    title_jpn = Column(String)
    category = Column(Enum(Category))
    thumb_url = Column(String, ForeignKey('thumb.url'))
    uploader = Column(String)
    posted_at = Column(DateTime(timezone=False))
    filecount = Column(SmallInteger)
    filesize = Column(BigInteger)
    expunged = Column(Boolean)
    # Hundredths of a star (0-500); multiply by 0.01 when reading.
    rating = Column(SmallInteger)

    first_gid = Column(BigInteger, ForeignKey('gallery.gid'))
    dupe_with = Column(BigInteger, ForeignKey('gallery.gid'), index=True)

    # Last metadata ingestion after the filtering age; NULL until then.
    # Download and deduplication state changes must not advance this timestamp.
    updated_at = Column(DateTime(timezone=False), index=True)
    # Last successful comparison using this gallery as the base. Refreshed
    # metadata (updated_at > deduped_at) requires another comparison.
    deduped_at = Column(DateTime(timezone=False))

    bittorrent = Column(Enum(DownloadState), nullable=False, default=DownloadState.QUEUED, server_default='QUEUED')
    archiver = Column(Enum(DownloadState), nullable=False, default=DownloadState.QUEUED, server_default='QUEUED')
    bittorrent_task_id = Column(String)
    bittorrent_started_at = Column(DateTime(timezone=False))
    archiver_task_id = Column(String)

    tags = relationship('Tag', secondary=gallery_tag, back_populates='galleries')
    thumb = relationship('Thumb', back_populates='galleries')
    torrents = relationship('Torrent', back_populates='gallery')

    __table_args__ = (
        Index('ix_gallery_first_gid_gid', 'first_gid', 'gid'),
        Index('ix_gallery_thumb_url_gid', 'thumb_url', 'gid'),
        Index('ix_gallery_dedupe_filecount_gid', 'filecount', 'gid',
              postgresql_where=dupe_with.is_(None) & title.isnot(None)),
        Index('ix_gallery_dedupe_gid', 'gid',
              postgresql_where=(dupe_with.is_(None) & title.isnot(None)
                                & (deduped_at.is_(None) | (deduped_at < updated_at)))),
        Index('ix_gallery_queued_gid', 'gid', postgresql_where=(
            dupe_with.is_(None) & title.isnot(None)
            & (archiver == DownloadState.QUEUED)
            & bittorrent.notin_([DownloadState.COMPLETED, DownloadState.DOWNLOADING])
        )),
        Index('ix_gallery_bittorrent_gid', 'gid',
              postgresql_where=bittorrent == DownloadState.DOWNLOADING),
        Index('ix_gallery_archiver_gid', 'archiver', 'gid', postgresql_where=(
            (archiver == DownloadState.DOWNLOADING)
            | (archiver.in_([DownloadState.COMPLETED, DownloadState.DOWNLOAD_FAILED,
                            DownloadState.VALIDATION_FAILED])
               & archiver_task_id.isnot(None))
        )),
    )


class Thumb(Base):
    __tablename__ = 'thumb'

    url = Column(String, primary_key=True)
    mobile_net_v3 = Column(Vector(576))

    galleries = relationship('Gallery', back_populates='thumb')

    __table_args__ = (
        Index('ix_thumb_cosine_hnsw', 'mobile_net_v3', postgresql_using='hnsw',
              postgresql_ops={'mobile_net_v3': 'vector_cosine_ops'}).ddl_if(dialect='postgresql'),
        Index('ix_thumb_unvectorized_url', 'url',
              postgresql_where=mobile_net_v3.is_(None)),
    )


class Tag(Base):
    __tablename__ = 'tag'

    id = Column(Integer, primary_key=True, autoincrement=True)
    namespace = Column(String, nullable=False)
    name = Column(String, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint('namespace', 'name', name='uq_tag_namespace_name'),
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

    gid = Column(BigInteger, ForeignKey('gallery.gid'), primary_key=True)
    infohash = Column(String, primary_key=True)
    added_at = Column(DateTime(timezone=False))
    name = Column(String)
    tsize = Column(BigInteger)
    fsize = Column(BigInteger)

    gallery = relationship('Gallery', back_populates='torrents')
