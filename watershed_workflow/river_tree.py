"""Module for working with tree data structures, built on watershed_workflow.tinytree"""
import logging
import collections
import numpy as np
import itertools
from scipy.spatial import cKDTree

import shapely.geometry
import shapely.ops

import watershed_workflow.utils
import watershed_workflow.tinytree


_tol = 1.e-7

class River(watershed_workflow.tinytree.Tree):
    """A tree node data structure"""
    def __init__(self, segment=None, children=None):
        """Do not call me.  Instead use the class factory methods, one of:

        - construct_rivers_by_geometry() # generic data
        - construct_rivers_by_hydroseq() # NHDPlus data

        This method initializes a single node in the River,
        representing one reach and its upstream children.

        """
        super(River, self).__init__(children)
        self.segment = segment

        if hasattr(self.segment, 'properties'):
            self.properties = self.segment.properties
        else:
            self.properties = dict()

    def addChild(self, segment):
        """Append a child (upstream) reach to this reach."""
        if type(segment) is River:
            super(River,self).addChild(segment)
        else:
            super(River,self).addChild(type(self)(segment))
        return self.children[-1]

    def dfs(self):
        """Iterates of reaches in the river in an "upstream-first" or "depth-first" ordering."""
        for node in self.preOrder():
            if node.segment is not None:
                yield node.segment

    def leaf_nodes(self):
        """Generator for all leaves of the tree."""
        for it in self.preOrder():
            if len(it.children) == 0 and it.segment is not None:
                yield it

    def leaves(self):
        """Generator for all leaf reaches of the tree."""
        for n in self.leaf_nodes():
            yield n.segment

    def __len__(self):
        """Number of total reaches in the river."""
        return sum(1 for i in self.dfs())

    def __iter__(self):
        return self.dfs()

    def _is_consistent(self, child, tol=_tol):
        """Is a given child consistent."""
        if not watershed_workflow.utils.close(child.segment.coords[-1], self.segment.coords[0], tol):
            return False
        return True
    
    def is_consistent(self, tol=_tol):
        """Checks geometric consistency of the river.

        Confirms that all upstream children's downstream coordinate
        coincides with self's upstream coordinate.
        """
        return all(self._is_consistent(child, tol) for child in self.children)

    def get_inconsistent(self, tol=_tol):
        """Returns a list of inconsistent children."""
        inconsistent = []
        for child in self.children:
            if not self._is_consistent(child, tol):
                logging.warning("  INCONSISTENT:")
                logging.warning("    child: %r"%(child.segment.coords[:]))
                logging.warning("    parent: %r"%(self.segment.coords[:]))
                inconsistent.append(child)
        return inconsistent


    #
    # Factory functions
    #
    @classmethod
    def construct_rivers_by_geometry(cls, reaches, tol=_tol):
        """Forms a list of River trees from a list of reaches by looking for
        close endpoints of those reaches.

        Note that this expects that endpoints of a reach coincide with
        beginpoints of their downstream reach, and does not work for
        cases where the junction is at a midpoint of a reach.
        """
        logging.debug("Generating Rivers")

        if len(reaches) == 0:
            return list()

        # make a kdtree of beginpoints
        coords = np.array([r.coords[0] for r in reaches])
        kdtree = cKDTree(coords)

        # make a node for each segment
        nodes = [cls(r) for r in reaches]

        # match nodes to their parent through the kdtree
        rivers = []
        divergence = []
        divergence_matches = []
        for j,n in enumerate(nodes):
            # find the closest beginpoint the this node's endpoint
            closest = kdtree.query_ball_point(n.segment.coords[-1], tol)
            if len(closest) > 1:
                logging.debug("Bad multi segment:")
                logging.debug(" connected to %d: %r"%(j,list(n.segment.coords[-1])))
                divergence.append(j)
                divergence_matches.append(closest)

                # end at the same point, pick the min angle deviation
                my_tan = np.array(n.segment.coords[-1]) - np.array(n.segment.coords[-2])
                my_tan = my_tan / np.linalg.norm(my_tan)

                other_tans = [np.array(reaches[c].coords[1]) - np.array(reaches[c].coords[0]) for c in closest]
                other_tans = [ot/np.linalg.norm(ot) for ot in other_tans]
                dots = [np.inner(ot,my_tan) for ot in other_tans]
                for i,c in enumerate(closest):
                    logging.debug("  %d: %r --> %r with dot product = %g"%(c,coords[c],reaches[c].coords[-1], dots[i]))
                c = closest[np.argmax(dots)]
                nodes[c].addChild(n)

            elif len(closest) == 0:
                rivers.append(n)
            else:
                assert(len(closest) == 1)
                nodes[closest[0]].addChild(n)

        assert(len(rivers) > 0)
        return rivers


    @classmethod
    def construct_rivers_by_hydroseq(cls, segments):
        """Given a list of segments, create a list of rivers using the
        HydroSeq maps provided in NHDPlus datasets.
        """
        # create a map of all segments from HydroSeqID to segment
        hydro_seq_ids = dict((seg.properties['HydrologicSequence'], cls(seg)) for seg in segments)

        roots = []
        for hs_id, node in hydro_seq_ids.items():
            down_hs_id = node.properties['DownstreamMainPathHydroSeq']
            try:
                hydro_seq_ids[down_hs_id].addChild(node)
            except KeyError:
                roots.append(node)
        return roots
    


