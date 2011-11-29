#python imports
import os
#dulwich inports
from dulwich.repo import (
	SYMREF,
	BaseRepo,
	RefsContainer as BaseRefsContainer,
)
from dulwich.object_store import PackBasedObjectStore
from dulwich.pack import (
	Pack as DulwichPack,
	PackIndex as DulwichPackIndex,
	PackData as DulwichPackData,
	ThinPackData,
	write_pack_data,
)
from dulwich.objects import (
	ShaFile,
	sha_to_hex,
)
from dulwich.errors import (
    MissingCommitError,
    NoIndexPresent,
    NotBlobError,
    NotCommitError,
    NotGitRepository,
    NotTreeError,
    NotTagError,
    PackedRefsException,
    CommitError,
    RefFormatError,
    ChecksumMismatch
   )
#appengine imports
from google.appengine.ext import (
	db,
	blobstore,
)
from google.appengine.api import files
#python imports
from StringIO import StringIO
import logging

#baseRepo
class Repositories(db.Model):
	pass

class NamedFiles(db.Model):
	repository = db.ReferenceProperty(Repositories)
	filename = db.StringProperty()
	contents = db.TextProperty()

class Repo(BaseRepo):
	"""
		Class for repositories on google appengine
		:param RepoName: String, open the repo with name String
	"""
	def __init__(self, RepoName):
		repo = Repositories.get_by_key_name(RepoName)
		if repo:
			self.REPO_NAME = RepoName
			self.Bare = True
			object_store = ObjectStore(RepoName)
			refs_container = RefsContainer(RepoName)
			BaseRepo.__init__(self, object_store, refs_container)
		else:
			raise NotGitRepository(RepoName)
	
	def get_named_file(self, fname):
		"""
			get a file from the database keyed by path
			the table is set out similar to a key value table
			:param fname: the key of a key value pair
		"""

		repo = Repositories.get_by_key_name(self.REPO_NAME)
		obj = db.Query(NamedFiles)
		obj.filter('repository =', repo)
		obj.filter('filename =', fname)

		if obj.count(1): #exists
			f = obj.get()
			return StringIO(f.contents)
		else:
			return None

	def _put_named_file(self, fname, contents):
		"""
			save a file in the datastore containing contents and keyed by path
			the table is set out similar to a key value table
			:param fname: the key
			:param contents: the value corresponding to key
		"""
		repo = Repositories.get_by_key_name(self.REPO_NAME)
		obj = db.Query(NamedFiles)
		obj.filter('repository =', repo)
		obj.filter('filename =', fname)

		if obj.count(1): #exists
			f = obj.get()
			f.contents = contents
			f.put()
		else:
			NamedFiles(
				repository = Repositories.get_by_key_name(self.REPO_NAME).key(),
				filename = fname,
				contents = contents,
			).put()

	def head(self):
		"""
			return the sha pointed at head
			:return: string hex encoded sha
		"""
		HEAD = self.refs['HEAD']
		return HEAD

	def open_index(self):
		"""
			this is a bare repo which doesn't have an index
			an index is used in a working repo to keep a record of changes to files,
			thus saving git having to scan all the files for changes at commit time
			I'm assuming the index is updated when running git add
			
			as this is a server, it does not have a working tree and hence does not have an index
		"""
		raise NoIndexPresent()
	
	@classmethod
	def init_bare(cls, RepoName):
		"""
			creates a new repository with name RepoName
			:param RepoName: the name of the new repo
			:return: a AppengineRepo class object
		"""
		Repositories(
			key_name = RepoName,
		).put()
		repo = cls(RepoName)
		repo.refs.set_symbolic_ref("HEAD", "refs/heads/master")
		repo._init_files(bare=True)
		return repo
		

#object/pack store
#objects reference the blob, not the other way round
class PackStore(db.Model):
	repository = db.ReferenceProperty(Repositories)
	sha1 = db.StringListProperty()
	data = blobstore.BlobReferenceProperty()
	size = db.IntegerProperty()
	checksum = db.BlobProperty()

class PackStoreIndex(db.Model):
	"""
		This is needed, pack indexes are stored in a separate file. This table is that separate file
		The indexes appear to be a cache (of sorts) as the index data can be created from the pack data
	"""
	packref = db.ReferenceProperty(PackStore)
	sha = db.StringProperty() #@TODO: should be a binary property, the sha is not hex encoded
	offset = db.IntegerProperty()
	crc32 = db.IntegerProperty()

class ObjectStore(PackBasedObjectStore):
	""" object store interface """
	def __init__(self, REPO_NAME):
		"""
			specifies which repository the object store should use
			:param REPO_NAME: the name of the repository object_store should access
		"""
		self.REPO_NAME = REPO_NAME
		self.REPO = Repositories.get_by_key_name(self.REPO_NAME)
	
	def contains_loose(self, sha):
		"""
			returns true if an object exists
			as all objects are stored in packs this will be false
		"""
		return False
	
	def _iter_loose_objects(self):
		"""Iterate over the SHAs of all loose objects."""
		raise NotImplementedError(self._iter_loose_objects)

	def _get_loose_object(self, sha):
		raise NotImplementedError(self._get_loose_object)

	def _remove_loose_object(self, sha):
		raise NotImplementedError(self._remove_loose_object)
	
	def contains_packed(self, sha):
		"""
			returns true if an object is stored inside a pack
		"""
		obj = db.Query(PackStoreIndex)
		obj.filter('repository =', self.REPO)
		obj.filter('sha1 =', sha)
		if( obj.count(1)):
			return True
		else:
			return False

	def __iter__(self):
		"""iterate over all sha1s in the objects table"""
		repo = Repositories.get_by_key_name(self.REPO_NAME)
		q = db.Query(PackStoreIndex)
		q.filter('repository = ', repo)
		#i'm fairly sure the GAE db.Query object is an iterator hence we can just return the instance
		return q.__iter__()

	#implemented by parent class
	@property
	def packs(self):
		"""
			Returns a list of dulwich.pack.Pack()
			these would be generated from the datastore blobs
			-- this will be a very high cost query --
		"""
		return []

	def get_raw_old(self, name):
		"""return a tuple containing the numeric type and object contents"""
		"""
			:return: string
		"""
		"""numeric type -> type_name : type_num
			commit	: 1
			tree	: 2
			blob	: 3
			tag	: 4
		"""
		if(len(name)==20):
			name = sha_to_hex(name)
		obj = self._query(name)
		count = obj.count(1)
		if count:
			obj = obj.get()
			""" @todo:
				appengine always returns a long for type_num
				the dict below is a stupid way to convert a long to an integer
				because using python functions to convert between a long and an int
				are giving me trouble
				CURRENTLY: this horrible implementation works and there are other things that need fixing
			"""
			a = int(obj.type_num)
			a = {
				1:1,
				2:2,
				3:3,
				4:4,
			}.get(obj.type_num)
			return a, str(obj.data)
		else:
			raise KeyError(name)

	def get_raw(self, name):
		"""
			return a tuple containing the numeric type and object contents
			:param name: the sha1 of the object
		"""
		if(len(name)==20):
			name = sha_to_hex(name)
		query = db.Query(PackStoreIndex)
		#query.filter("repository =", self.REPO)
		query.filter("sha =", name)
		if query.count(1):
			obj = query.get()
			p = Pack(obj.packref)
			output = p.get_raw(name)
			return output
		else:
			raise KeyError(name)

	def add_object(self, obj):
		"""
			adds a single object to the datastore
			we should only be getting thin packs
			the packs will be converted to full pack and be added through add_objects
		"""
		logging.error("call to add object")
		raise CommitError
		PackStoreIndex(
			repository = Repositories.get_by_key_name(self.REPO_NAME).key(),
			sha1 = obj.id,
			data = obj.as_raw_string(),
			type_num = obj.type_num,
		).put()

	def add_objects(self, objects):
		#get the pack blobstore key
		for o in objects:
			PackStoreIndex(
				repository = Repositories.get_by_key_name(self.REPO_NAME),
				sha1 = o.id,
				type_num = o.type_num,
#				packdata = #blobstore key
			).save()

	def add_thin_pack(self):
		"""
			A pack is a single file containing multiple objects
			A pack contains a list of references to objects inside the pack
			this is done to save parsing through the entire file to find all the objects
			
			The difference between a pack and thin pack is thin packs contain references
			to objects which may not be stored in the pack, rather git must refer to the repository
			which contains the referenced object as either a loose object or a pack
			
			DiskObjectStore, which I used as a reference for this function creates a full pack.
			Here I extract all the objects and store them as loose objects in the datastore, similar to
			how memory object store works
		"""
		fileContents = StringIO("")
		def newcommit():
			try:
				#write the new pack
				logging.error('starting the write')
				#creating a copy of fileContents is done to move the file pointer back to the beginning
				fileContents.seek(0,2)
				tempstring = StringIO(fileContents.getvalue())
				ThinPack = ThinPackData(self.get_raw, filename=None, file=tempstring, size=fileContents.tell())
				store = PackStore(repository = self.REPO)
				store.size = fileContents.tell()
				store.save()
				p = Pack.Create(store, ThinPack)
			except:
				import traceback
				traceback.print_exc()
				raise CommitError
			return p
		return fileContents, newcommit
	

class Pack(DulwichPack):
	"""
		What I want this class to do
			- return a dulwich.pack.Pack object from a blobstore key
			- generate a new pack dulwich.pack.Pack from a ThinPackData
	"""	
	@classmethod
	def Create(cls, pack_store, data):
		"""
			data is an instance of class ThinPackData(PackData) from dulwich.pack
		"""
		idx = PackIndex.create(pack_store, data)
		self = cls.from_objects(data, idx)
		
		f = StringIO()
		write_pack_data(f, ((o, None) for o in self.iterobjects()), len(self))
		#write data
		blob_name = files.blobstore.create(mime_type='application/octet-stream')
		with files.open(blob_name, 'a') as blob:
			blob.write(f.getvalue())
		files.finalize(blob_name)

		self.pack_store = pack_store
		self.pack_store.data = files.blobstore.get_blob_key(blob_name)
		self.pack_store.save()
		return self
	
	""" this was commented out for committing """
	def __init__(self, pack_store):
		super(Pack, self).__init__("")
		if pack_store != "": #This is to ensure Pack.FromObjects will work
			self.pack_store = pack_store
			blob_reader = blobstore.BlobReader(self.pack_store.data)
			self._data_load = lambda: PackData(filename=None, file=blob_reader, size=self.pack_store.size)
			self._idx_load = lambda: PackIndex(self.pack_store) #@TODO: I need to store the checksum somewhere


class PackData(DulwichPackData):
	pass

class PackIndex(DulwichPackIndex):
	"""Pack index that is stored entirely in memory."""
	@classmethod
	def create(cls, pack_store, pack_data):
		for sha, offset, crc32 in pack_data.iterentries():
			sha = sha_to_hex(sha)
			pack_store.sha1.append(sha)
			PackStoreIndex(
				packref = pack_store,
				sha = sha,
				offset = offset,
				crc32 = crc32,
			).save()
		t_checksum = pack_data.get_stored_checksum()
		pack_store.checksum=t_checksum
		pack_store.save()
		return cls(pack_store)
	
	def __init__(self, pack_store, pack_checksum=None):
		"""Create a new MemoryPackIndex.

		:param entries: Sequence of name, idx, crc32 (sorted)
		:param pack_checksum: Optional pack checksum
		"""
		self._by_sha = {}
		self._entries = []
		q = db.Query(PackStoreIndex)
		q.filter('packref =', pack_store)
		for obj in q:
			self._by_sha[obj.sha] = obj.offset
			self._entries.append( [obj.sha, obj.offset, obj.crc32] )
		self._pack_checksum = pack_store.checksum

	def get_pack_checksum(self):
		#@todo: this returns a blob type, should return a str type
		return self._pack_checksum

	def __len__(self):
		return len(self._entries)

	def object_index(self, sha):
		return self._by_sha[sha]

	def _itersha(self):
		return iter(self._by_sha)

	def iterentries(self):
		return iter(self._entries)
	
	def check(self):
		"""Check that the stored checksum matches the actual checksum."""
		logging.error("gae_backend.py -> PackIndex.Check()")
		return
		# taken from Pack.FilePackIndex
		#actual = self.calculate_checksum()
		#stored = self.get_stored_checksum()
		#if actual != stored:
		#	raise ChecksumMismatch(stored, actual)

class ThinPackExtractor(ThinPackData):
	"""
		This class is used to extract loose git objects out of the thin pack
		this should also work for standard git packs, although I have not tested it
		@todo: alot of work needs to be done here mainly ensuring that all data is extracted safetly
	"""
	def __init__(self, object_store, fileIO):
		"""self.resolve_ext_ref(sha) is created in ThinPackData"""
		"""self._file is created in ThinPackData"""
		self.object_store = object_store
		super(ThinPackExtractor, self).__init__(self.object_store.get_raw, filename=None, file=StringIO(fileIO.getvalue()))
		#this new stringIO business is due to me being stupid and not being able to figure out how to seek to the beginning of the string
		
	def extract(self):
		"""
			extracts each object from the pack
			checks if it is in the datastore
			if it is not in the datastore add it
		"""
		for entry in self.iterentries():
			sha = sha_to_hex(entry[0])
			obj_in_datastore = self.object_store.contains_loose(sha)
			if not obj_in_datastore:
				type_num, raw_chunks = self.get_object_at(entry[1])
				realObject = ShaFile.from_raw_chunks(type_num, raw_chunks)
				self.object_store.add_object(realObject)
			else:
				pass #i'll remove this after I finish debugging, but it helps me see program flow
	
	def get_size(self):
		"""	@todo: 
			returns the size of the object
			this probably will not work due to some weird things when using stringio
		"""
		if self._size == None:
			self._file.seek(0, os.SEEK_END) #os.SEEK_END simply returns 2
			self._size = self._file.tell()
			self._size = self._file.len
		return self._size
	
	def check(self):
		logging.error("NOT IMPLEMENTED: gae_backend.py ThinPackExtractor.check(): this should verify the objects have been written correctly")
		"""
			#@todo: 
			#we need to verify that everything was written correctly
			#raise ChecksumMismatch if something is wrong
			#look in pack.py Pack.check() for an example
			#after the data is checked we can probably call self.close()
		"""
	
	def close(self):
		"""
			closes the file apparantly clearing up memory
		"""
		self._file.close()


#RefsContainer
class References(db.Model):
	repository = db.ReferenceProperty(Repositories)
	ref = db.StringProperty()
	pointer = db.StringProperty() #this can be an sha1 or a to another ref

class RefsContainer(BaseRefsContainer):
	def __init__(self, RepoName):
		self.REPO_NAME = RepoName
	
	def _query(self, ref=None):
		repo = Repositories.get_by_key_name(self.REPO_NAME)
		q = db.Query(References)
		if ref != None:
			q.filter('ref =', ref)
		q.filter('repository =', repo)
		return q
	
	def allkeys(self):
		#this is returning incorrect data and needs fixing
		#	the above comment is not comforting
		#	I have a feeling this was fixed, but as the comment remains I need to verify this
		keys = []
		q = self._query()
		for k in q:
			keys.append(k.ref)
		return keys
	
	def read_loose_ref(self, name):
		"""
			returns the target of a reference
			this function does not follow symbolic refs
			:name string: the name of the reference
		"""
		refs = self._query(name)
		if refs.count(1):
			tref = refs.get()
			tpointer = tref.pointer
			return tpointer
		else:
			return None

	def get_packed_refs(self):
		"""
			refs stores inside a pack
			we don't use packs so return an empty dict
		"""
		return {}
	
	def set_symbolic_ref(self, name, other):
		"""
			refs usually point at objects,
			however it is possible for a ref to point at another ref
			an example is HEAD
			
			:name string: the name of the ref
			:other string: the target of this ref (what the reference points at).
		"""
		References(
			repository = Repositories.get_by_key_name(self.REPO_NAME),
			ref=name,
			pointer=SYMREF+other,
		).put()
	
	def set_if_equals(self, name, old_ref, new_ref):
		"""if old_ref is none we continue
		if refs[name]== old_ref we continue
		else we false"""
		realReference = self._follow(name)
		if old_ref == None or realReference[1] == old_ref:
			query = self._query(name)
			ref = query.get()
			if ref == None:
				ref = References(
					repository = Repositories.get_by_key_name(self.REPO_NAME),
					ref = name,
				)
			ref.pointer = new_ref
			ref.put()
			return True
		else:
			return False
	
	def add_if_new(self, name, new):
		""" add a reference if it doesn't exist """
		if self._query(name).get() == None:
			References(
				repository = Repositories.get_by_key_name(self.REPO_NAME),
				ref = name,
				pointer = new,
			).put()
		
	def remove_if_equals(self, name, old_ref):
		""" remove a reference """
		ref = self._query(name).get()
		if ref == None:
			return False
		if old_ref == None or ref.pointer == old_ref:
			ref.delete()
			return True
		else:
			return False
		