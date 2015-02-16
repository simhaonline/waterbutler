import os
import asyncio
import json

import requests

from waterbutler.core import utils
from waterbutler.core import streams
from waterbutler.core import provider
from waterbutler.core import exceptions

from waterbutler.providers.box import settings
from waterbutler.providers.box.metadata import BoxRevision
from waterbutler.providers.box.metadata import BoxFileMetadata
from waterbutler.providers.box.metadata import BoxFolderMetadata

from waterbutler.providers.box import utils as box_utils


class BoxPath(utils.WaterButlerPath):

    def __init__(self, path, prefix=False, suffix=False):
        super().__init__(path, prefix=prefix, suffix=suffix)
        if path != '/':
            self._id = path.split('/')[1]
        else:
            self._id = path

    def __repr__(self):
        return "{}({!r})".format(self.__class__.__name__, self._orig_path)


class BoxProvider(provider.BaseProvider):

    BASE_URL = settings.BASE_URL
    BASE_UPLOAD_URL = settings.BASE_UPLOAD_URL

    def __init__(self, auth, credentials, settings):
        super().__init__(auth, credentials, settings)
        self.token = self.credentials['token']
        self.folder = self.settings['folder']

    @property
    def default_headers(self):
        return {
            'Authorization': 'Bearer {}'.format(self.token),
        }

    @asyncio.coroutine
    def intra_copy(self, dest_provider, source_options, dest_options):
        source_path = BoxPath(self.folder, source_options['path'])
        dest_path = BoxPath(self.folder, dest_options['path'])
        if self == dest_provider:
            resp = yield from self.make_request(
                'POST',
                self.build_url(source_path.path, 'copy'),
                data={
                    'folder': 'auto',
                    'from_path': source_path.path,
                    'to_path': dest_path._id,
                },
                expects=(200, 201),
                throws=exceptions.IntraCopyError,
            )
        else:
            from_ref_resp = yield from self.make_request(
                'POST',
                self.build_url('copy_ref', 'auto', source_path.path),
            )
            from_ref_data = yield from from_ref_resp.json()
            resp = yield from self.make_request(
                'POST',
                data={
                    'root': 'auto',
                    'from_copy_ref': from_ref_data['copy_ref'],
                    'to_path': dest_path,
                },
                headers=dest_provider.default_headers,
                expects=(200, 201),
                throws=exceptions.IntraCopyError,
            )
        data = yield from resp.json()
        return BoxFileMetadata(self.folder, data).serialized()

    @asyncio.coroutine 
    def intra_move(self, dest_provider, source_options, dest_options):
        source_path = BoxPath(self.folder, source_options['path'])
        dest_path = BoxPath(self.folder, dest_options['path'])
        resp = yield from self.make_request(
            'POST',
            self.build_url('fileops', 'move'),
            data={
                'root': 'auto',
                'from_path': source_path.path,
                'to_path': dest_path.path,
            },
            expects=(200, ),
            throws=exceptions.IntraMoveError,
        )
        data = yield from resp.json()
        return BoxFileMetadata(data, self.folder).serialized()

    @asyncio.coroutine # ✓
    def download(self, path, revision=None, **kwargs):
        path = BoxPath(path)
        resp = yield from self.make_request(
            'GET',
            self.build_url('files', path._id, 'content'),
            expects=(200, ),
            throws=exceptions.DownloadError,
        )
        return streams.ResponseStreamReader(resp)

    @asyncio.coroutine # ✓
    def upload(self, stream, path, **kwargs):
        path = BoxPath('/'+self.folder+path)
        try:
            meta = yield from self.metadata(str(path))
        except exceptions.MetadataError:
            created = True
            data = yield from self._upload_create(stream, path)
        else:
            created = False
            data = yield from self._upload_update(stream, path, meta)

        return BoxFileMetadata(data['entries'][0], self.folder).serialized(), created

    @asyncio.coroutine # ✓
    def delete(self, path, **kwargs):
        #'etag' of the file can be included as an ‘If-Match’ header to prevent race conditions

        path = BoxPath(path)
        # A metadata call will verify the path specified is actually the
        # requested file or folder.
        yield from self.metadata(str(path))

        yield from self.make_request(
            'DELETE',
            self.build_url('files', path._id),
            #data={'root': 'auto', 'path': path.path},
            expects=(204, ),
            throws=exceptions.DeleteError,
        )

    @asyncio.coroutine # ✓
    def metadata(self, path, **kwargs):
        path = BoxPath(path)
        uid = self.folder

        if path.is_file:
            return self._get_file_meta(uid, path)
        else:
            return self._get_folder_meta(uid, path)


    @asyncio.coroutine
    def revisions(self, path, **kwargs):  
        #from https://developers.box.com/docs/#files-view-versions-of-a-file :
        #Alert: Versions are only tracked for Box users with premium accounts.
        #Most users won't have a premium account, fail quietly if not
        path = BoxPath(path)
        response = yield from self.make_request(
            'GET',
            self.build_url('files', path._id, 'versions'),
            expects=(200, ),
            throws=exceptions.RevisionsError
        )
        data = yield from response.json()

        idx = 1
        ret = []
        while(data['entries']):
            item = data['entries'].pop()
            item['revision'] = idx
            idx += 1
            ret.append(BoxRevision(item, folder=self.folder).serialized())

        curr = yield from self.metadata(str(path))
        curr['revision'] = idx
        ret.append(BoxRevision(curr, folder=self.folder).serialized())

        #import ipdb; ipdb.set_trace()

        return ret
#        return [
#            BoxRevision(item, folder=self.folder).serialized()
#            for item in data['entries']
#        ]

    def can_intra_copy(self, dest_provider):
        return type(self) == type(dest_provider)

    def can_intra_move(self, dest_provider):
        return self.can_intra_copy(dest_provider)

    def _get_file_meta(self, uid, path): # ✓
        resp = yield from self.make_request(
            'GET',
            self.build_url('folders', uid, 'items'),
            expects=(200, ),
            throws=exceptions.MetadataError
        )

        data = yield from resp.json()

        for item in data['entries']:
            if item['type']=='file':
                if item['name']==path.name:
                    return BoxFileMetadata(item, self.folder).serialized()
        
        raise exceptions.MetadataError('Unable to find file.')

    def _get_folder_meta(self, uid, path): # ✓
        resp = yield from self.make_request(
            'GET',
            self.build_url('folders', uid, 'items'),
            expects=(200, ),
            throws=exceptions.MetadataError
        )

        data = yield from resp.json()

        ret = []
        for item in data['entries']:
            if item['type']=='folder':
                ret.append(BoxFolderMetadata(item, self.folder).serialized())
            else:
                ret.append(BoxFileMetadata(item, self.folder).serialized())
        return ret

    def _upload_create(self, stream, path): # ✓
        stream, boundary, size = box_utils.make_upload_data(stream, parent_id=path._id, file=path.name)
        resp = yield from self.make_request(
            'POST',
            self._build_upload_url('files', 'content'),
            data=stream,
            headers={
                'Content-Length': str(size),
                'Content-Type': 'multipart/form-data; boundary={0}'.format(boundary.decode()),
            },
            expects=(200, 201, ),
            throws=exceptions.UploadError,
        )
        data = yield from resp.json()
        return data

    def _upload_update(self, stream, path, meta): # ✓
        #'etag' of the file can be included as an ‘If-Match’ header to prevent race conditions
        meta_path = BoxPath(meta['path'])
        stream, boundary, size = box_utils.make_upload_data(stream, parent_id=path._id, file=path.name)
        resp = yield from self.make_request(
            'POST',
            self._build_upload_url('files', meta_path._id, 'content'),
            data=stream,
            headers={
                'Content-Length': str(size),
                'Content-Type': 'multipart/form-data; boundary={0}'.format(boundary.decode()),
            },
            expects=(200, 201, ),
            throws=exceptions.UploadError,
        )
        data = yield from resp.json()
        return data


    def _build_upload_url(self, *segments, **query): # ✓
        return provider.build_url(settings.BASE_UPLOAD_URL, *segments, **query)