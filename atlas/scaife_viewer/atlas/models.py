import io
import re
from collections import defaultdict

from django.core import serializers
from django.db import models
from django.utils.functional import cached_property

# @@@ optional for Django 3.1+
from django_jsonfield_backport.models import JSONField
from graphene_django.utils import camelize
from sortedm2m.fields import SortedManyToManyField
from treebeard.mp_tree import MP_Node

from scaife_viewer.atlas import constants
from scaife_viewer.atlas.conf import settings

from .hooks import hookset


class TextAlignment(models.Model):
    """
    Tracks an alignment between one or more texts.
    """

    label = models.CharField(blank=True, null=True, max_length=255)
    description = models.TextField(blank=True, null=True)

    # TODO: Formalize CITE data model for alignments
    urn = models.CharField(max_length=255, unique=True)

    """
    metadata contains author / attribution information
    """
    metadata = JSONField(default=dict, blank=True)

    """
    versions being sorted maps onto the "items" within a particular record
    """
    versions = SortedManyToManyField(
        "scaife_viewer_atlas.Node", related_name="text_alignments"
    )

    def __str__(self):
        return self.label


class TextAlignmentRecord(models.Model):
    """
    Maps to the AlignmentRecord generated by Ducat
    """

    urn = models.CharField(max_length=255, unique=True)
    metadata = JSONField(default=dict, blank=True)

    idx = models.IntegerField(help_text="0-based index")

    alignment = models.ForeignKey(
        "scaife_viewer_atlas.TextAlignment",
        related_name="records",
        on_delete=models.CASCADE,
    )
    # TODO: Denorm "text part" nodes

    class Meta:
        ordering = ["idx"]


class TextAlignmentRecordRelation(models.Model):
    # TODO: Enforce this as the "lowest" work component
    version = models.ForeignKey("scaife_viewer_atlas.Node", on_delete=models.CASCADE)
    record = models.ForeignKey(
        "scaife_viewer_atlas.TextAlignmentRecord",
        on_delete=models.CASCADE,
        related_name="relations",
    )
    tokens = models.ManyToManyField(
        "scaife_viewer_atlas.Token", related_name="alignment_record_relations"
    )


class TextAnnotation(models.Model):
    kind = models.CharField(
        max_length=255,
        default=hookset.TEXT_ANNOTATION_DEFAULT_KIND,
        choices=hookset.TEXT_ANNOTATION_KIND_CHOICES,
    )
    data = JSONField(default=dict, blank=True)
    idx = models.IntegerField(help_text="0-based index")

    text_parts = SortedManyToManyField(
        "scaife_viewer_atlas.Node", related_name="text_annotations"
    )

    urn = models.CharField(max_length=255, blank=True, null=True)

    def resolve_references(self):
        if "references" not in self.data:
            print(f'No references found [urn="{self.urn}"]')
            return
        desired_urns = set(self.data["references"])
        reference_objs = list(Node.objects.filter(urn__in=desired_urns))
        resolved_urns = set([r.urn for r in reference_objs])
        delta_urns = desired_urns.symmetric_difference(resolved_urns)

        if delta_urns:
            print(
                f'Could not resolve all references, probably due to bad data in the CEX file [urn="{self.urn}" unresolved_urns="{",".join(delta_urns)}"]'
            )
        self.text_parts.set(reference_objs)


class MetricalAnnotation(models.Model):
    # @@@ in the future, we may ingest any attributes into
    # `data` and query via JSON
    data = JSONField(default=dict, blank=True)

    html_content = models.TextField()
    short_form = models.TextField(
        help_text='"|" indicates the start of a foot, ":" indicates a syllable boundary within a foot and "/" indicates a caesura.'
    )

    idx = models.IntegerField(help_text="0-based index")
    text_parts = SortedManyToManyField(
        "scaife_viewer_atlas.Node", related_name="metrical_annotations"
    )

    urn = models.CharField(max_length=255, blank=True, null=True)

    @property
    def metrical_pattern(self):
        """
        alias of foot_code; could be denormed if we need to query
        """
        return self.data["foot_code"]

    @property
    def line_num(self):
        return self.data["line_num"]

    @property
    def foot_code(self):
        return self.data["foot_code"]

    @property
    def line_data(self):
        return self.data["line_data"]

    def generate_html(self):
        buffer = io.StringIO()
        print(
            f'        <div class="line {self.foot_code}" id="line-{self.line_num}" data-meter="{self.foot_code}">',
            file=buffer,
        )
        print("          <div>", end="", file=buffer)
        index = 0
        for foot in self.foot_code:
            if foot == "a":
                syllables = self.line_data[index : index + 3]
                index += 3
            else:
                syllables = self.line_data[index : index + 2]
                index += 2
            if syllables[0]["word_pos"] in [None, "r"]:
                print("\n            ", end="", file=buffer)
            print('<span class="foot">', end="", file=buffer)
            for i, syllable in enumerate(syllables):
                if i > 0 and syllable["word_pos"] in [None, "r"]:
                    print("\n            ", end="", file=buffer)
                syll_classes = ["syll"]
                if syllable["length"] == "long":
                    syll_classes.append("long")
                if syllable["caesura"]:
                    syll_classes.append("caesura")
                if syllable["word_pos"] is not None:
                    syll_classes.append(syllable["word_pos"])
                syll_class_string = " ".join(syll_classes)
                print(
                    f'<span class="{syll_class_string}">{syllable["text"]}</span>',
                    end="",
                    file=buffer,
                )
            print("</span>", end="", file=buffer)
        print("\n          </div>", file=buffer)
        print("        </div>", file=buffer)
        buffer.seek(0)
        return buffer.read().strip()

    def generate_short_form(self):
        """
        |μῆ:νιν :ἄ|ει:δε :θε|ὰ /Πη|λη:ϊ:ά|δεω :Ἀ:χι|λῆ:ος
        """
        index = 0
        form = ""
        for foot in self.foot_code:
            if foot == "a":
                syllables = self.line_data[index : index + 3]
                index += 3
            else:
                syllables = self.line_data[index : index + 2]
                index += 2
            form += "|"
            for i, syllable in enumerate(syllables):
                if i > 0 and syllable["word_pos"] in [None, "r"]:
                    form += " "
                if syllable["caesura"]:
                    form += "/"
                elif i > 0:
                    form += ":"
                form += syllable["text"]
        return form

    def resolve_references(self):
        if "references" not in self.data:
            print(f'No references found [urn="{self.urn}"]')
            return
        desired_urns = set(self.data["references"])
        reference_objs = list(Node.objects.filter(urn__in=desired_urns))
        resolved_urns = set([r.urn for r in reference_objs])
        delta_urns = desired_urns.symmetric_difference(resolved_urns)

        if delta_urns:
            print(
                f'Could not resolve all references [urn="{self.urn}" unresolved_urns="{",".join(delta_urns)}"]'
            )
        self.text_parts.set(reference_objs)


IMAGE_ANNOTATION_KIND_CANVAS = "canvas"
IMAGE_ANNOTATION_KIND_CHOICES = ((IMAGE_ANNOTATION_KIND_CANVAS, "Canvas"),)


class ImageAnnotation(models.Model):
    kind = models.CharField(
        max_length=7,
        default=IMAGE_ANNOTATION_KIND_CANVAS,
        choices=IMAGE_ANNOTATION_KIND_CHOICES,
    )
    data = JSONField(default=dict, blank=True)
    # @@@ denormed from data
    image_identifier = models.CharField(max_length=255, blank=True, null=True)
    canvas_identifier = models.CharField(max_length=255, blank=True, null=True)
    idx = models.IntegerField(help_text="0-based index")

    text_parts = SortedManyToManyField(
        "scaife_viewer_atlas.Node", related_name="image_annotations"
    )

    urn = models.CharField(max_length=255, blank=True, null=True)


class ImageROI(models.Model):
    data = JSONField(default=dict, blank=True)

    # @@@ denormed from data; could go away when Django's SQLite backend has proper
    # JSON support
    image_identifier = models.CharField(max_length=255)
    # @@@ this could be structured
    coordinates_value = models.CharField(max_length=255)
    # @@@ idx
    image_annotation = models.ForeignKey(
        "scaife_viewer_atlas.ImageAnnotation",
        related_name="roi",
        on_delete=models.CASCADE,
    )

    text_parts = SortedManyToManyField("scaife_viewer_atlas.Node", related_name="roi")
    text_annotations = SortedManyToManyField(
        "scaife_viewer_atlas.TextAnnotation", related_name="roi"
    )


class AudioAnnotation(models.Model):
    data = JSONField(default=dict, blank=True)
    asset_url = models.URLField(max_length=200)
    idx = models.IntegerField(help_text="0-based index")

    text_parts = SortedManyToManyField(
        "scaife_viewer_atlas.Node", related_name="audio_annotations"
    )

    urn = models.CharField(max_length=255, blank=True, null=True)

    def resolve_references(self):
        if "references" not in self.data:
            print(f'No references found [urn="{self.urn}"]')
            return
        desired_urns = set(self.data["references"])
        reference_objs = list(Node.objects.filter(urn__in=desired_urns))
        resolved_urns = set([r.urn for r in reference_objs])
        delta_urns = desired_urns.symmetric_difference(resolved_urns)

        if delta_urns:
            print(
                f'Could not resolve all references, probably due to bad data in the CEX file [urn="{self.urn}" unresolved_urns="{",".join(delta_urns)}"]'
            )
        self.text_parts.set(reference_objs)


# TODO: Review https://docs.djangoproject.com/en/3.0/topics/db/multi-db/
# to see if there are more settings we can expose for "mixed"
# database backends
class Node(MP_Node):
    # @@@ used to pivot siblings; may be possible if we hook into path field
    idx = models.IntegerField(help_text="0-based index", blank=True, null=True)
    # @@@ if we expose kind, can access some GraphQL enumerations
    kind = models.CharField(max_length=255)
    urn = models.CharField(max_length=255, unique=True)
    ref = models.CharField(max_length=255, blank=True, null=True)
    rank = models.IntegerField(blank=True, null=True)
    text_content = models.TextField(blank=True, null=True)
    # @@@ we may want to furthe de-norm label from metadata
    metadata = JSONField(default=dict, blank=True, null=True)

    alphabet = settings.SV_ATLAS_NODE_ALPHABET

    def __str__(self):
        return f"{self.kind}: {self.urn}"

    @property
    def label(self):
        return self.metadata.get("label", self.urn)

    @property
    def lsb(self):
        """
        An alias for lowest citation part, preserved for
        backwards-comptability with scaife-viewer/scaife-viewer
        https://github.com/scaife-viewer/scaife-viewer/blob/e6974b2835918741acca781c39f46fd79d5406c9/scaife_viewer/cts/passage.py#L58
        """
        return self.lowest_citabale_part

    @property
    def lowest_citable_part(self):
        """
        Returns the lowest part of the URN's citation

        # @@@ may denorm this for performance
        """
        if not self.rank:
            return None
        return self.ref.split(".").pop()

    @classmethod
    def dump_tree(cls, root=None, up_to=None, to_camel=True):
        """Dump a tree or subtree for serialization rendering all
        fieldnames as camelCase by default.

        Extension of django-treebeard.treebeard.mp_tree `dump_bulk` for
        finer-grained control over the initial queryset and resulting value.
        """
        if up_to and up_to not in constants.CTS_URN_NODES:
            raise ValueError(f"Invalid CTS node identifier for: {up_to}")

        # NOTE: This filters the queryset using path__startswith,
        # because the default `get_tree(parent=root)` uses `self.is_leaf
        # and the current bulk ingestion into ATLAS does not populate
        # `numchild`.
        qs = cls._get_serializable_model().get_tree()
        if root:
            qs = qs.filter(
                path__startswith=root.path,
                # depth__gte=parent.depth
            ).order_by("path")
        if up_to:
            depth = constants.CTS_URN_DEPTHS[up_to]
            qs = qs.exclude(depth__gt=depth)

        tree, index = [], {}
        for pyobj in serializers.serialize("python", qs):
            fields = pyobj["fields"]
            path = fields["path"]
            depth = int(len(path) / cls.steplen)
            del fields["depth"]
            del fields["path"]
            del fields["numchild"]

            metadata = fields["metadata"]
            if to_camel:
                fields = camelize(fields)
                metadata = camelize(metadata)
            fields.update({"metadata": metadata})

            newobj = {"data": fields}

            if (not root and depth == 1) or (root and len(path) == len(root.path)):
                tree.append(newobj)
            else:
                parentpath = cls._get_basepath(path, depth - 1)
                parentobj = index[parentpath]
                if "children" not in parentobj:
                    parentobj["children"] = []
                parentobj["children"].append(newobj)
            index[path] = newobj
        return tree

    def get_refpart_siblings(self, version):
        """
        Node.get_siblings assumes siblings at the same position in the Node
        heirarchy.

        Refpart siblings crosses over parent boundaries, e.g.
        considers 1.611 and 2.1 as siblings.
        """
        if not self.rank:
            return Node.objects.none()
        return version.get_descendants().filter(rank=self.rank)

    def get_descendants(self):
        # NOTE: This overrides `get_descendants` to avoid checking
        # `self.is_leaf`; current bulk ingestion into ATLAS
        # does not populate numchild.
        # TODO: populate numchild and remove override
        parent = self
        return (
            self.__class__.objects.filter(
                path__startswith=parent.path, depth__gte=parent.depth
            )
            .order_by("path")
            .exclude(pk=parent.pk)
        )

    def get_children(self):
        # NOTE: This overrides `get_children` to avoid checking
        # `self.is_leaf`; current bulk ingestion into ATLAS
        # does not populate numchild.
        # TODO: populate numchild and remove override

        return self.__class__.objects.filter(
            depth=self.depth + 1,
            path__range=self._get_children_path_interval(self.path),
        ).order_by("path")


class Token(models.Model):
    text_part = models.ForeignKey(
        "Node", related_name="tokens", on_delete=models.CASCADE
    )

    value = models.CharField(
        max_length=255,
        help_text="the tokenized value of a text part (usually whitespace separated)",
    )
    # @@@ consider JSON or EAV to store / filter attrs
    word_value = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="the normalized version of the value (no punctuation)",
    )
    subref_value = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="the value for the CTS subreference targeting a particular token",
    )
    lemma = models.CharField(
        max_length=255, blank=True, null=True, help_text="the lemma for the token value"
    )
    gloss = models.CharField(
        max_length=255, blank=True, null=True, help_text="the interlinear gloss"
    )
    part_of_speech = models.CharField(max_length=255, blank=True, null=True)
    tag = models.CharField(
        max_length=255, blank=True, null=True, help_text="part-of-speech tag"
    )
    case = models.CharField(max_length=255, blank=True, null=True)
    mood = models.CharField(max_length=255, blank=True, null=True)

    position = models.IntegerField()
    idx = models.IntegerField(help_text="0-based index")

    ve_ref = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="a human-readable reference to the token via a virtualized exemplar",
    )

    @staticmethod
    def get_word_value(value):
        return re.sub(r"[^\w]", "", value)

    @classmethod
    def tokenize(cls, text_part_node, counters):
        # @@@ compare with passage-based tokenization on
        # scaife-viewer/scaife-viewer.  See discussion on
        # https://github.com/scaife-viewer/scaife-viewer/issues/162
        #
        # For this implementation, we always calculate the index
        # within the text part, _not_ the passage. Also see
        # http://www.homermultitext.org/hmt-doc/cite/cts-subreferences.html
        idx = defaultdict(int)
        pieces = text_part_node.text_content.split()
        to_create = []
        for pos, piece in enumerate(pieces):
            # @@@ the word value will discard punctuation or
            # whitespace, which means we only support "true"
            # subrefs for word tokens
            w = cls.get_word_value(piece)
            wl = len(w)
            for wk in (w[i : j + 1] for i in range(wl) for j in range(i, wl)):
                idx[wk] += 1
            subref_idx = idx[w]
            subref_value = f"{w}[{subref_idx}]"

            position = pos + 1
            to_create.append(
                cls(
                    text_part=text_part_node,
                    value=piece,
                    word_value=w,
                    position=position,
                    ve_ref=f"{text_part_node.ref}.t{position}",
                    idx=counters["token_idx"],
                    subref_value=subref_value,
                )
            )
            counters["token_idx"] += 1
        return to_create

    def __str__(self):
        return f"{self.text_part.urn} :: {self.value}"


class NamedEntity(models.Model):
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    kind = models.CharField(max_length=6, choices=constants.NAMED_ENTITY_KINDS)
    url = models.URLField(max_length=200)
    data = JSONField(default=dict, blank=True)

    idx = models.IntegerField(help_text="0-based index", blank=True, null=True)
    urn = models.CharField(max_length=255, unique=True)

    # @@@ we may also want structure these references using URNs
    tokens = models.ManyToManyField(
        "scaife_viewer_atlas.Token", related_name="named_entities"
    )

    def __str__(self):
        return f"{self.urn} :: {self.title }"


class Dictionary(models.Model):
    """
    A dictionary model.
    """

    label = models.CharField(blank=True, null=True, max_length=255)
    data = JSONField(default=dict, blank=True)

    urn = models.CharField(
        max_length=255, unique=True, help_text="urn:cite2:<site>:dictionaries.atlas_v1"
    )

    def __str__(self):
        return self.label


class Repo(models.Model):
    """
    NOTE: consider other modeling options like a 1 to 1 model or denorms
    to different work-level components of the URN
    """

    name = models.CharField(blank=True, null=True, max_length=255)
    sha = models.CharField(blank=True, null=True, max_length=255)
    urns = models.ManyToManyField("scaife_viewer_atlas.Node", related_name="repos")
    metadata = JSONField(default=dict, blank=True)


class AttributionPerson(models.Model):
    name = models.CharField(max_length=255)
    # TODO: Consider a CITE URN as well
    orcid_id = models.URLField(max_length=36, blank=True, null=True)  # U


class AttributionOrganization(models.Model):
    name = models.CharField(max_length=255)
    # TODO: Consider a CITE URN as well
    url = models.URLField(max_length=255, blank=True, null=True)  # U


class AttributionRecord(models.Model):
    # TODO: Denorm role into data field
    role = models.CharField(max_length=255)

    person = models.ForeignKey(
        "scaife_viewer_atlas.AttributionPerson",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
    )
    organization = models.ForeignKey(
        "scaife_viewer_atlas.AttributionOrganization",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
    )

    # TODO: Enforce person or organization constraint

    data = JSONField(default=dict, blank=True)
    # NOTE:
    # data --> references are CTS URNs (maybe database field is too)
    # data --> annotations are CITE URNs (also maybe further modeled in the database)

    # TODO: Formalize relation patterns; we'll query through data.references
    # to begin

    # TODO: IDX
    urns = models.ManyToManyField(
        "scaife_viewer_atlas.Node", related_name="attribution_records"
    )

    @cached_property
    def name(self):
        """
        Provides a shortcut for the person / organization related to
        the record
        """
        parts = []
        if self.person:
            parts.append(self.person.name)
        if self.organization:
            parts.append(self.organization.name)
        return ", ".join(parts)


class DictionaryEntry(models.Model):
    headword = models.CharField(max_length=255)
    headword_normalized = models.CharField(max_length=255, blank=True, null=True)
    data = JSONField(default=dict, blank=True)

    idx = models.IntegerField(help_text="0-based index")
    urn = models.CharField(
        max_length=255, unique=True, help_text="urn:cite2:<site>:entries.atlas_v1"
    )

    dictionary = models.ForeignKey(
        "scaife_viewer_atlas.Dictionary",
        related_name="entries",
        on_delete=models.CASCADE,
    )


class Sense(MP_Node):
    label = models.CharField(blank=True, null=True, max_length=255)
    definition = models.CharField(blank=True, null=True, max_length=255)

    alphabet = settings.SV_ATLAS_NODE_ALPHABET

    idx = models.IntegerField(help_text="0-based index", blank=True, null=True)
    urn = models.CharField(
        max_length=255, unique=True, help_text="urn:cite2:<site>:senses.atlas_v1"
    )

    entry = models.ForeignKey(
        "scaife_viewer_atlas.DictionaryEntry",
        related_name="senses",
        on_delete=models.CASCADE,
    )


class Citation(models.Model):
    label = models.CharField(blank=True, null=True, max_length=255)
    idx = models.IntegerField(help_text="0-based index", blank=True, null=True)
    urn = models.CharField(
        max_length=255, unique=True, help_text="urn:cite2:<site>:citations.atlas_v1"
    )
    sense = models.ForeignKey(
        "scaife_viewer_atlas.Sense", related_name="citations", on_delete=models.CASCADE,
    )
    data = JSONField(default=dict, blank=True)
    # TODO: There may be additional optimizations we can do on the text part / citation relation
    text_parts = SortedManyToManyField(
        "scaife_viewer_atlas.Node", related_name="sense_citations"
    )


# TODO: Determine how strict we want to be on object vs value; need object type for entry.texts


METADATA_VISIBILITY_ALL = "all"
METADATA_VISIBILITY_LIBRARY = "library"
METADATA_VISIBILITY_READER = "reader"
METADATA_VISIBILITY_HIDDEN = "hidden"
METADATA_VISIBLITY_CHOICES = [
    (METADATA_VISIBILITY_ALL, "all"),
    (METADATA_VISIBILITY_LIBRARY, "library"),
    (METADATA_VISIBILITY_READER, "reader"),
    (METADATA_VISIBILITY_HIDDEN, "hidden"),
]


class Metadata(models.Model):
    idx = models.IntegerField(help_text="0-based index", blank=True, null=True)
    urn = models.CharField(
        # TODO: Can we encode the collection into the URN too?
        max_length=255,
        unique=True,
        help_text="urn:cite2:<site>:metadata.atlas_v1",
    )
    collection_urn = models.CharField(
        max_length=255, help_text="urn:cite2:<site>:metadata_collection.atlas_v1"
    )
    datatype = models.CharField(
        # TODO: Object vs CITEObj, etc
        choices=[
            ("str", "String"),
            ("int", "Integer"),
            ("date", "Date"),
            ("obj", "Object"),
            ("cite_urn", "CITE URN"),
        ],
        max_length=8,
        default="str",
    )
    label = models.CharField(max_length=255)
    value = models.CharField(blank=True, null=True, max_length=255)
    value_obj = JSONField(default=dict, blank=True)

    index = models.BooleanField(default=True, help_text="Include in search index")
    visibility = models.CharField(
        choices=METADATA_VISIBLITY_CHOICES,
        max_length=7,
        default=METADATA_VISIBILITY_ALL,
    )

    level = models.CharField(
        choices=[
            ("text_group", "Text Group"),
            ("work", "Work"),
            ("version", "Version"),
            ("passage", "Passage"),
        ],
        max_length=11,
        default="version",
        help_text="Human-readable representation of the level of URN(s) to which metadata is attached",
    )
    # TODO: Decouple level and depth, but likely refactoring depth
    depth = models.PositiveIntegerField()

    cts_relations = SortedManyToManyField(
        "scaife_viewer_atlas.Node", related_name="metadata_records"
    )

    def __str__(self):
        return f"{self.label}: {self.value}"
