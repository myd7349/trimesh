import collections
import numpy as np
import uuid
import io

from .. import util
from .. import graph

from ..constants import log

try:
    import networkx as nx
except BaseException as E:
    # create a dummy module which will raise the ImportError
    # or other exception only when someone tries to use networkx
    from ..exceptions import ExceptionModule
    nx = ExceptionModule(E)


def load_3MF(file_obj,
             postprocess=True,
             **kwargs):
    """
    Load a 3MF formatted file into a Trimesh scene.

    Parameters
    ------------
    file_obj : file-like
      Contains 3MF formatted data

    Returns
    ------------
    kwargs : dict
      Constructor arguments for `trimesh.Scene`
    """
    # dict, {name in archive: BytesIo}
    archive = util.decompress(file_obj, file_type='zip')
    # get model file
    model = archive['3D/3dmodel.model']

    # read root attributes only from XML first
    event, root = next(etree.iterparse(model, tag=('{*}model'), events=('start',)))
    # collect unit information from the tree
    if 'unit' in root.attrib:
        metadata = {'units': root.attrib['unit']}
    else:
        # the default units, defined by the specification
        metadata = {'units': 'millimeters'}

    # { mesh id : mesh name}
    id_name = {}
    # { mesh id: (n,3) float vertices}
    v_seq = {}
    # { mesh id: (n,3) int faces}
    f_seq = {}
    # components are objects that contain other objects
    # {id : [other ids]}
    components = collections.defaultdict(list)
    # load information about the scene graph
    # each instance is a single geometry
    build_items = []

    consumed_names = set()
    # iterate the XML object and build elements with an LXML iterator
    # loaded elements are cleared to avoid ballooning memory
    model.seek(0)
    for event, obj in etree.iterparse(model, tag=('{*}object', '{*}build')):
        # parse objects
        if 'object' in obj.tag:
            # id is mandatory
            index = obj.attrib['id']

            # start with stored name
            name = obj.attrib.get('name', str(index))
            # apparently some exporters name multiple meshes
            # the same thing so check to see if it's been used
            if name in consumed_names:
                name = name + str(index)
            consumed_names.add(name)
            # store name reference on the index
            id_name[index] = name

            # if the object has actual geometry data parse here
            for mesh in obj.iter('{*}mesh'):
                vertices = mesh.find('{*}vertices')
                v_seq[index] = np.array([[i.attrib['x'],
                                          i.attrib['y'],
                                          i.attrib['z']] for
                                         i in vertices.iter('{*}vertex')],
                                        dtype=np.float64)
                vertices.clear()
                vertices.getparent().remove(vertices)

                faces = mesh.find('{*}triangles')
                f_seq[index] = np.array([[i.attrib['v1'],
                                          i.attrib['v2'],
                                          i.attrib['v3']] for
                                         i in faces.iter('{*}triangle')],
                                        dtype=np.int64)
                faces.clear()
                faces.getparent().remove(faces)

            # components are references to other geometries
            for c in obj.iter('{*}component'):
                mesh_index = c.attrib['objectid']
                transform = _attrib_to_transform(c.attrib)
                components[index].append((mesh_index, transform))

        # parse build
        if 'build' in obj.tag:
            # scene graph information stored here, aka "build" the scene
            for item in obj.iter('{*}item'):
                # get a transform from the item's attributes
                transform = _attrib_to_transform(item.attrib)
                # the index of the geometry this item instantiates
                build_items.append((item.attrib['objectid'], transform))

        # free resources
        obj.clear()
        obj.getparent().remove(obj)
        del obj

    # have one mesh per 3MF object
    # one mesh per geometry ID, store as kwargs for the object
    meshes = {}
    for gid in v_seq.keys():
        name = id_name[gid]
        meshes[name] = {'vertices': v_seq[gid],
                        'faces': f_seq[gid],
                        'metadata': metadata.copy()}
        meshes[name].update(kwargs)

    # turn the item / component representation into
    # a MultiDiGraph to compound our pain
    g = nx.MultiDiGraph()
    # build items are the only things that exist according to 3MF
    # so we accomplish that by linking them to the base frame
    for gid, tf in build_items:
        g.add_edge('world', gid, matrix=tf)
    # components are instances which need to be linked to base
    # frame by a build_item
    for start, group in components.items():
        for i, (gid, tf) in enumerate(group):
            g.add_edge(start, gid, matrix=tf)

    # turn the graph into kwargs for a scene graph
    # flatten the scene structure and simplify to
    # a single unique node per instance
    graph_args = []
    parents = collections.defaultdict(set)
    for path in graph.multigraph_paths(G=g,
                                       source='world'):
        # collect all the transform on the path
        transforms = graph.multigraph_collect(G=g,
                                              traversal=path,
                                              attrib='matrix')
        # combine them into a single transform
        if len(transforms) == 1:
            transform = transforms[0]
        else:
            transform = util.multi_dot(transforms)

        # the last element of the path should be the geometry
        last = path[-1][0]
        # if someone included an undefined component, skip it
        if last not in id_name:
            log.debug('id {} included but not defined!'.format(last))
            continue
        # frame names unique
        name = id_name[last] + util.unique_id()
        # index in meshes
        geom = id_name[last]

        # collect parents if we want to combine later
        if len(path) > 2:
            parent = path[-2][0]
            parents[parent].add(last)

        graph_args.append({'frame_from': 'world',
                           'frame_to': name,
                           'matrix': transform,
                           'geometry': geom})

    # solidworks will export each body as its own mesh with the part
    # name as the parent so optionally rename and combine these bodies
    if postprocess and all('body' in i.lower() for i in meshes.keys()):
        # don't rename by default
        rename = {k: k for k in meshes.keys()}
        for parent, mesh_name in parents.items():
            # only handle the case where a parent has a single child
            # if there are multiple children we would do a combine op
            if len(mesh_name) != 1:
                continue
            # rename the part
            rename[id_name[next(iter(mesh_name))]] = id_name[parent].split(
                '(')[0]

        # apply the rename operation meshes
        meshes = {rename[k]: m for k, m in meshes.items()}
        # rename geometry references in the scene graph
        for arg in graph_args:
            if 'geometry' in arg:
                arg['geometry'] = rename[arg['geometry']]

    # construct the kwargs to load the scene
    kwargs = {'base_frame': 'world',
              'graph': graph_args,
              'geometry': meshes,
              'metadata': metadata}

    return kwargs


def export_3MF(mesh):
    """
    Converts a Trimesh object into a 3MF file.

    Parameters
    ---------
    mesh: Trimesh object

    Returns
    ---------
    export: bytes, representing mesh in 3MF form.
    """
    # collect geometry from scenes or single mesh
    geometry = mesh.geometry if hasattr(mesh, "geometry") else {"object": mesh}

    # 3mf archive dict {path: BytesIO}
    archive = {}

    # 3dmodel.model (xml stream to BytesIO)
    archive["3D/3dmodel.model"] = f = io.BytesIO()
    with etree.xmlfile(f, encoding="utf-8") as xf:
        xf.write_declaration()
        # xml namespaces
        nsmap = {
            None: "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
            "m": "http://schemas.microsoft.com/3dmanufacturing/material/2015/02",
            "p": "http://schemas.microsoft.com/3dmanufacturing/production/2015/06",
            "b": "http://schemas.microsoft.com/3dmanufacturing/beamlattice/2017/02",
            "s": "http://schemas.microsoft.com/3dmanufacturing/slice/2015/07",
            "sc": "http://schemas.microsoft.com/3dmanufacturing/securecontent/2019/04",
        }

        # stream elements
        with xf.element("model", {"unit": "millimeter"}, nsmap=nsmap):
            with xf.element("resources"):
                for i, (name, m) in enumerate(geometry.items()):
                    attribs = {"id": str(i + 1), "name": name, "type": "model", "p:UUID": str(uuid.uuid4())}
                    with xf.element("object", **attribs):
                        with xf.element("mesh"):
                            with xf.element("vertices"):
                                for v1, v2, v3 in m.vertices:
                                    xf.write(etree.Element("vertex", x=str(v1), y=str(v2), z=str(v3)))
                            with xf.element("triangles"):
                                for f1, f2, f3 in m.faces:
                                    xf.write(etree.Element("triangle", v1=str(f1), v2=str(f2), v3=str(f3)))

            with xf.element("build", {"p:UUID": str(uuid.uuid4())}):
                for i in range(len(geometry)):
                    xf.write(
                        etree.Element(
                            "item", {"objectid": str(i + 1), f"{{{nsmap['p']}}}UUID": str(uuid.uuid4())}, nsmap=nsmap
                        )
                    )
    f.seek(0)

    # .rels
    archive["_rels/.rels"] = f = io.BytesIO()
    with etree.xmlfile(f, encoding="utf-8") as xf:
        xf.write_declaration()
        # xml namespaces
        nsmap = {None: "http://schemas.openxmlformats.org/package/2006/relationships"}

        # stream elements
        with xf.element("Relationships", nsmap=nsmap):
            xf.write(
                etree.Element(
                    "Relationship",
                    Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel",
                    Target="/3D/3dmodel.model",
                    Id="rel0",
                )
            )
    f.seek(0)

    # [Content_Types].xml
    archive["[Content_Types].xml"] = f = io.BytesIO()
    with etree.xmlfile(f, encoding="utf-8") as xf:
        xf.write_declaration()
        # xml namespaces
        nsmap = {None: "http://schemas.openxmlformats.org/package/2006/content-types"}

        # stream elements
        types = [
            ("jpeg", "image/jpeg"),
            ("jpg", "image/jpeg"),
            ("model", "application/vnd.ms-package.3dmanufacturing-3dmodel+xml"),
            ("png", "image/png"),
            ("rels", "application/vnd.openxmlformats-package.relationships+xml"),
            ("texture", "application/vnd.ms-package.3dmanufacturing-3dmodeltexture"),
        ]
        with xf.element("Types", nsmap=nsmap):
            for ext, ctype in types:
                xf.write(etree.Element("Default", Extension=ext, ContentType=ctype))
    f.seek(0)

    return util.compress(archive)


def _attrib_to_transform(attrib):
    """
    Extract a homogeneous transform from a dictionary.

    Parameters
    ------------
    attrib: dict, optionally containing 'transform'

    Returns
    ------------
    transform: (4, 4) float, homogeonous transformation
    """

    transform = np.eye(4, dtype=np.float64)
    if 'transform' in attrib:
        # wangle their transform format
        values = np.array(
            attrib['transform'].split(),
            dtype=np.float64).reshape((4, 3)).T
        transform[:3, :4] = values
    return transform


# do import here to keep lxml a soft dependency
try:
    from lxml import etree
    _three_loaders = {'3mf': load_3MF}
except ImportError:
    _three_loaders = {}
