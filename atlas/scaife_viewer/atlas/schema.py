import os

from django.db.models import Q

import django_filters
from graphene import Boolean, Connection, Field, ObjectType, String, relay
from graphene.types import generic
from graphene_django import DjangoObjectType
from graphene_django.filter import DjangoFilterConnectionField
from graphene_django.utils import camelize

from . import constants

# @@@ ensure convert signal is registered
from .compat import convert_jsonfield_to_string  # noqa
from .hooks import hookset
from .language_utils import normalize_string

# from .models import Node as TextPart
from .models import (
    AttributionRecord,
    AudioAnnotation,
    Citation,
    Dictionary,
    DictionaryEntry,
    ImageAnnotation,
    Metadata,
    MetricalAnnotation,
    NamedEntity,
    Node,
    Repo,
    Sense,
    TextAlignment,
    TextAlignmentRecord,
    TextAlignmentRecordRelation,
    TextAnnotation,
    Token,
)
from .passage import (
    PassageMetadata,
    PassageOverviewMetadata,
    PassageSiblingMetadata,
)
from .utils import (
    extract_version_urn_and_ref,
    filter_via_ref_predicate,
    get_textparts_from_passage_reference,
)


# TODO: Make these proper, documented configuration variables
RESOLVE_CITATIONS_VIA_TEXT_PARTS = bool(
    int(os.environ.get("SV_ATLAS_RESOLVE_CITATIONS_VIA_TEXT_PARTS", 1))
)
RESOLVE_DICTIONARY_ENTRIES_VIA_LEMMAS = bool(
    int(os.environ.get("SV_ATLAS_RESOLVE_DICTIONARY_ENTRIES_VIA_LEMMAS", 0))
)

# @@@ alias Node because relay.Node is quite different
TextPart = Node


class LimitedConnectionField(DjangoFilterConnectionField):
    """
    Ensures that queries without `first` or `last` return up to
    `max_limit` results.
    """

    @classmethod
    def connection_resolver(
        cls,
        resolver,
        connection,
        default_manager,
        max_limit,
        enforce_first_or_last,
        filterset_class,
        filtering_args,
        root,
        info,
        **resolver_kwargs,
    ):
        first = resolver_kwargs.get("first")
        last = resolver_kwargs.get("last")
        if not first and not last:
            resolver_kwargs["first"] = max_limit
        return super(LimitedConnectionField, cls).connection_resolver(
            resolver,
            connection,
            default_manager,
            max_limit,
            enforce_first_or_last,
            filterset_class,
            filtering_args,
            root,
            info,
            **resolver_kwargs,
        )


class PassageOverviewNode(ObjectType):
    all_top_level = generic.GenericScalar(
        name="all", description="Inclusive list of top-level text parts for a passage"
    )
    selected = generic.GenericScalar(
        description="Only the selected top-level objects for a given passage"
    )

    class Meta:
        description = (
            "Provides lists of top-level text part objects for a given passage"
        )

    @staticmethod
    def resolve_all_top_level(obj, info, **kwargs):
        return obj.all

    @staticmethod
    def resolve_selected(obj, info, **kwargs):
        return obj.selected


class PassageSiblingsNode(ObjectType):
    # @@@ dry for resolving scalars
    all_siblings = generic.GenericScalar(
        name="all", description="Inclusive list of siblings for a passage"
    )
    selected = generic.GenericScalar(
        description="Only the selected sibling objects for a given passage"
    )
    previous = generic.GenericScalar(description="Siblings for the previous passage")
    next_siblings = generic.GenericScalar(
        name="next", description="Siblings for the next passage"
    )

    class Meta:
        description = "Provides lists of sibling objects for a given passage"

    def resolve_all_siblings(obj, info, **kwargs):
        return obj.all

    def resolve_selected(obj, info, **kwargs):
        return obj.selected

    def resolve_previous(obj, info, **kwargs):
        return obj.previous

    def resolve_next_siblings(obj, info, **kwargs):
        return obj.next


class PassageMetadataNode(ObjectType):
    human_reference = String()
    ancestors = generic.GenericScalar()
    overview = Field(PassageOverviewNode)
    siblings = Field(PassageSiblingsNode)
    children = generic.GenericScalar()
    next_passage = String(description="Next passage reference")
    previous_passage = String(description="Previous passage reference")
    healed_passage = String(description="Healed passage")

    def resolve_metadata(self, info, *args, **kwargs):
        # @@@
        return {}

    def resolve_previous_passage(self, info, *args, **kwargs):
        passage = info.context.passage
        if passage.previous_objects:
            return self.generate_passage_urn(passage.version, passage.previous_objects)

    def resolve_next_passage(self, info, *args, **kwargs):
        passage = info.context.passage
        if passage.next_objects:
            return self.generate_passage_urn(passage.version, passage.next_objects)

    def resolve_overview(self, info, *args, **kwargs):
        passage = info.context.passage
        # TODO: Review overview / ancestors / siblings implementation
        passage = info.context.passage
        return PassageOverviewMetadata(passage)

    def resolve_ancestors(self, info, *args, **kwargs):
        passage = info.context.passage
        return self.get_ancestor_metadata(passage.version, passage.start)

    def resolve_siblings(self, info, *args, **kwargs):
        passage = info.context.passage
        return PassageSiblingMetadata(passage)

    def resolve_children(self, info, *args, **kwargs):
        passage = info.context.passage
        return self.get_children_metadata(passage.start)

    def resolve_human_reference(self, info, *args, **kwargs):
        passage = info.context.passage
        return passage.human_readable_reference

    def resolve_healed_passage(self, info, *args, **kwargs):
        return getattr(info.context, "healed_passage_reference", None)


class PassageTextPartConnection(Connection):
    metadata = Field(PassageMetadataNode)

    class Meta:
        abstract = True

    def resolve_metadata(self, info, *args, **kwargs):
        passage = info.context.passage
        return PassageMetadata(passage)


# @@@ consider refactoring with TextPartsReferenceFilterMixin
class TextPartFilterSet(django_filters.FilterSet):
    reference = django_filters.CharFilter(method="reference_filter")

    def reference_filter(self, queryset, name, value):
        version_urn, ref = extract_version_urn_and_ref(value)
        start, end = ref.split("-")
        refs = [start]
        if end:
            refs.append(end)
        predicate = Q(ref__in=refs)
        queryset = queryset.filter(
            # @@@ this reference filter doesn't work because of
            # depth assumptions
            urn__startswith=version_urn,
            depth=len(start.split(".")) + 1,
        )
        return filter_via_ref_predicate(queryset, predicate)

    class Meta:
        model = TextPart
        fields = {
            "urn": ["exact", "startswith"],
            "ref": ["exact", "startswith"],
            "depth": ["exact", "lt", "gt"],
            "rank": ["exact", "lt", "gt"],
            "kind": ["exact"],
            "idx": ["exact"],
        }


def initialize_passage(gql_context, reference):
    """
    NOTE: graphene-django aliases request as info.context,
    but django-filter is wired to work off of a request.

    Where possible, we'll reference gql_context for consistency.
    """
    from scaife_viewer.atlas.backports.scaife_viewer.cts import passage_heal

    passage, healed = passage_heal(reference)
    gql_context.passage = passage
    if healed:
        gql_context.healed_passage_reference = passage.reference
    return passage.reference


class TextPartsReferenceFilterMixin:
    def get_lowest_textparts_queryset(self, value):
        value = initialize_passage(self.request, value)
        version = self.request.passage.version
        return get_textparts_from_passage_reference(value, version=version)


class PassageTextPartFilterSet(TextPartsReferenceFilterMixin, django_filters.FilterSet):
    reference = django_filters.CharFilter(method="reference_filter")

    class Meta:
        model = TextPart
        fields = []

    def reference_filter(self, queryset, name, value):
        return self.get_lowest_textparts_queryset(value)


class AbstractTextPartNode(DjangoObjectType):
    label = String()
    name = String()
    metadata = generic.GenericScalar()

    class Meta:
        abstract = True

    @classmethod
    def __init_subclass_with_meta__(cls, **meta_options):
        meta_options.update(
            {
                "model": TextPart,
                "interfaces": (relay.Node,),
                "filterset_class": TextPartFilterSet,
            }
        )
        super().__init_subclass_with_meta__(**meta_options)

    def resolve_metadata(obj, *args, **kwargs):
        return camelize(obj.metadata)


class TextGroupNode(AbstractTextPartNode):
    # @@@ work or version relations

    @classmethod
    def get_queryset(cls, queryset, info):
        return queryset.filter(depth=constants.CTS_URN_DEPTHS["textgroup"]).order_by(
            "pk"
        )

    # TODO: extract to AbstractTextPartNode
    def resolve_label(obj, *args, **kwargs):
        # @@@ consider a direct field or faster mapping
        return obj.metadata["label"]

    def resolve_metadata(obj, *args, **kwargs):
        metadata = obj.metadata
        return camelize(metadata)


class WorkNode(AbstractTextPartNode):
    # @@@ apply a subfilter here?
    versions = LimitedConnectionField(lambda: VersionNode)

    @classmethod
    def get_queryset(cls, queryset, info):
        return queryset.filter(depth=constants.CTS_URN_DEPTHS["work"]).order_by("pk")

    # TODO: extract to AbstractTextPartNode
    def resolve_label(obj, *args, **kwargs):
        # @@@ consider a direct field or faster mapping
        return obj.metadata["label"]

    def resolve_metadata(obj, *args, **kwargs):
        metadata = obj.metadata
        return camelize(metadata)


class RepoNode(DjangoObjectType):
    versions = LimitedConnectionField(lambda: VersionNode)
    metadata = generic.GenericScalar()

    class Meta:
        model = Repo
        interfaces = (relay.Node,)
        filter_fields = ["name"]

    def resolve_versions(obj, *args, **kwargs):
        return obj.urns

    def resolve_metadata(obj, *args, **kwargs):
        metadata = obj.metadata
        return camelize(metadata)


class VersionNode(AbstractTextPartNode):
    text_alignment_records = LimitedConnectionField(lambda: TextAlignmentRecordNode)

    access = Boolean()
    description = String()
    lang = String()
    human_lang = String()
    kind = String()

    @classmethod
    def get_queryset(cls, queryset, info):
        # TODO: set a default somewhere
        # return queryset.filter(kind="version").order_by("urn")
        return queryset.filter(depth=constants.CTS_URN_DEPTHS["version"]).order_by("pk")

    # TODO: Determine how tightly coupled these fields
    # should be to metadata (including ["key"] vs .get("key"))
    def resolve_access(obj, info, *args, **kwargs):
        request = info.context
        return hookset.can_access_urn(request, obj.urn)

    def resolve_human_lang(obj, *args, **kwargs):
        lang = obj.metadata["lang"]
        return hookset.get_human_lang(lang)

    def resolve_lang(obj, *args, **kwargs):
        return obj.metadata["lang"]

    def resolve_description(obj, *args, **kwargs):
        # @@@ consider a direct field or faster mapping
        return obj.metadata["description"]

    def resolve_kind(obj, *args, **kwargs):
        # @@@ consider a direct field or faster mapping
        return obj.metadata["kind"]

    # TODO: extract to AbstractTextPartNode
    def resolve_label(obj, *args, **kwargs):
        # @@@ consider a direct field or faster mapping
        return obj.metadata["label"]

    # TODO: convert metadata to proper fields
    def resolve_metadata(obj, *args, **kwargs):
        metadata = obj.metadata
        work = obj.get_parent()
        text_group = work.get_parent()
        metadata.update(
            {
                "work_label": work.label,
                "text_group_label": text_group.label,
                "lang": metadata["lang"],
                "human_lang": hookset.get_human_lang(metadata["lang"]),
            }
        )
        return camelize(metadata)


class TextPartNode(AbstractTextPartNode):
    lowest_citable_part = String()


class PassageTextPartNode(DjangoObjectType):
    label = String()

    class Meta:
        model = TextPart
        interfaces = (relay.Node,)
        connection_class = PassageTextPartConnection
        filterset_class = PassageTextPartFilterSet


class TreeNode(ObjectType):
    tree = generic.GenericScalar()

    def resolve_tree(obj, info, **kwargs):
        return obj


class TextAlignmentFilterSet(TextPartsReferenceFilterMixin, django_filters.FilterSet):
    reference = django_filters.CharFilter(method="reference_filter")

    class Meta:
        model = TextAlignment
        fields = ["label", "urn"]

    def reference_filter(self, queryset, name, value):
        textparts_queryset = self.get_lowest_textparts_queryset(value)
        # TODO: we may wish to further denorm relations to textparts
        # OR query based on the version, rather than the passage reference
        return queryset.filter(
            records__relations__tokens__text_part__in=textparts_queryset
        ).distinct()


class TextAlignmentNode(DjangoObjectType):
    metadata = generic.GenericScalar()

    class Meta:
        model = TextAlignment
        interfaces = (relay.Node,)
        filterset_class = TextAlignmentFilterSet

    def resolve_metadata(obj, info, *args, **kwargs):
        # TODO: make generic.GenericScalar derived class
        # that automatically camelizes data
        return camelize(obj.metadata)

    # TODO: from metadata, handle renderer property hint


class TextAlignmentRecordFilterSet(
    TextPartsReferenceFilterMixin, django_filters.FilterSet
):
    reference = django_filters.CharFilter(method="reference_filter")

    class Meta:
        model = TextAlignmentRecord
        fields = ["idx", "alignment", "alignment__urn"]

    def reference_filter(self, queryset, name, value):
        textparts_queryset = self.get_lowest_textparts_queryset(value)
        # TODO: Refactor as a manager method
        # TODO: Evaluate performance / consider a TextPart denorm on relations
        return queryset.filter(
            relations__tokens__text_part__in=textparts_queryset
        ).distinct()


# TODO: Where do these nested non-Django objects live in the project?
# Saelor favors <app>/types and <app>/schema; may revisit as we hit 1k LOC here
class TextAlignmentMetadata(dict):
    def get_passage_reference(self, version_urn, text_parts_list):
        refs = [text_parts_list[0].ref]
        last_ref = text_parts_list[-1].ref
        if last_ref not in refs:
            refs.append(last_ref)
        refpart = "-".join(refs)
        return f"{version_urn}{refpart}"

    def generate_passage_reference(self, version_urn, tokens_qs):
        tokens_list = list(
            tokens_qs.filter(text_part__urn__startswith=version_urn).order_by("idx")
        )
        text_parts_list = list(
            TextPart.objects.filter(tokens__in=tokens_list).distinct()
        )
        return {
            "reference": self.get_passage_reference(version_urn, text_parts_list),
            "start_idx": tokens_list[0].idx,
            "end_idx": tokens_list[-1].idx,
        }

    @property
    def passage_references(self):
        references = []
        alignment_records = list(self["alignment_records"])
        if not alignment_records:
            return references

        tokens_qs = Token.objects.filter(
            alignment_record_relations__record__in=alignment_records
        )

        # TODO: What does the order look like when we "start"
        # from the "middle" of a three-way alignment?
        # As it is now, we will start with the supplied reference
        # and then loop through the remaining, which could do weird
        # things for the order of "versions"
        version_urn, ref = extract_version_urn_and_ref(self["passage"].reference)
        references.append(self.generate_passage_reference(version_urn, tokens_qs))

        alignment = TextAlignment.objects.get(urn=self["alignment_urn"])
        for version in alignment.versions.exclude(urn=version_urn):
            references.append(self.generate_passage_reference(version.urn, tokens_qs))
        return references


class TextAlignmentMetadataNode(ObjectType):
    passage_references = generic.GenericScalar(
        description="References for the passages being aligned"
    )

    def resolve_passage_references(self, info, *args, **kwargs):
        return self.passage_references


class TextAlignmentConnection(Connection):
    metadata = Field(TextAlignmentMetadataNode)

    class Meta:
        abstract = True

    def get_alignment_urn(self, info):
        NAME_ALIGNMENT_URN = "alignment_Urn"
        aligmment_urn = info.variable_values.get("alignmentUrn")
        if aligmment_urn:
            return aligmment_urn

        for selection in info.operation.selection_set.selections:
            for argument in selection.arguments:
                if argument.name.value == NAME_ALIGNMENT_URN:
                    return argument.value.value

        raise Exception(
            f"{NAME_ALIGNMENT_URN} argument is required to retrieve metadata"
        )

    def resolve_metadata(self, info, *args, **kwargs):
        alignment_urn = self.get_alignment_urn(info)
        return TextAlignmentMetadata(
            **{
                "passage": info.context.passage,
                "alignment_records": self.iterable,
                "alignment_urn": alignment_urn,
            }
        )


class TextAlignmentRecordNode(DjangoObjectType):
    class Meta:
        model = TextAlignmentRecord
        interfaces = (relay.Node,)
        connection_class = TextAlignmentConnection
        filterset_class = TextAlignmentRecordFilterSet


class TextAlignmentRecordRelationNode(DjangoObjectType):
    class Meta:
        model = TextAlignmentRecordRelation
        interfaces = (relay.Node,)
        filter_fields = ["tokens__text_part__urn"]


class TextAnnotationFilterSet(TextPartsReferenceFilterMixin, django_filters.FilterSet):
    reference = django_filters.CharFilter(method="reference_filter")

    class Meta:
        model = TextAnnotation
        fields = ["urn"]

    def reference_filter(self, queryset, name, value):
        textparts_queryset = self.get_lowest_textparts_queryset(value)
        return queryset.filter(text_parts__in=textparts_queryset).distinct()


class AbstractTextAnnotationNode(DjangoObjectType):
    data = generic.GenericScalar()

    class Meta:
        abstract = True

    @classmethod
    def __init_subclass_with_meta__(cls, **meta_options):
        meta_options.update(
            {
                "model": TextAnnotation,
                "interfaces": (relay.Node,),
                "filterset_class": TextAnnotationFilterSet,
            }
        )
        super().__init_subclass_with_meta__(**meta_options)

    def resolve_data(obj, *args, **kwargs):
        return camelize(obj.data)


class TextAnnotationNode(AbstractTextAnnotationNode):
    # TODO: Eventually rename this as a scholia
    # annotation
    @classmethod
    def get_queryset(cls, queryset, info):
        return queryset.filter(kind=constants.TEXT_ANNOTATION_KIND_SCHOLIA)


class SyntaxTreeNode(AbstractTextAnnotationNode):
    @classmethod
    def get_queryset(cls, queryset, info):
        return queryset.filter(kind=constants.TEXT_ANNOTATION_KIND_SYNTAX_TREE)


class MetricalAnnotationNode(DjangoObjectType):
    data = generic.GenericScalar()
    metrical_pattern = String()

    class Meta:
        model = MetricalAnnotation
        interfaces = (relay.Node,)
        filter_fields = ["urn"]


class ImageAnnotationFilterSet(TextPartsReferenceFilterMixin, django_filters.FilterSet):
    reference = django_filters.CharFilter(method="reference_filter")

    class Meta:
        model = ImageAnnotation
        fields = ["urn"]

    def reference_filter(self, queryset, name, value):
        # Reference filters work at the lowest text parts, but we've chosen to
        # apply the ImageAnnotation :: TextPart link at the folio level.

        # Since individual lines are at the roi level, we query there.
        textparts_queryset = self.get_lowest_textparts_queryset(value)
        return queryset.filter(roi__text_parts__in=textparts_queryset).distinct()


class ImageAnnotationNode(DjangoObjectType):
    text_parts = LimitedConnectionField(lambda: TextPartNode)
    data = generic.GenericScalar()

    class Meta:
        model = ImageAnnotation
        interfaces = (relay.Node,)
        filterset_class = ImageAnnotationFilterSet


class AudioAnnotationNode(DjangoObjectType):
    data = generic.GenericScalar()

    class Meta:
        model = AudioAnnotation
        interfaces = (relay.Node,)
        filter_fields = ["urn"]


class TokenFilterSet(django_filters.FilterSet):
    class Meta:
        model = Token
        fields = {"text_part__urn": ["exact", "startswith"]}


class TokenNode(DjangoObjectType):
    class Meta:
        model = Token
        interfaces = (relay.Node,)
        filterset_class = TokenFilterSet


class NamedEntityFilterSet(TextPartsReferenceFilterMixin, django_filters.FilterSet):
    reference = django_filters.CharFilter(method="reference_filter")

    class Meta:
        model = NamedEntity
        fields = ["urn", "kind"]

    def reference_filter(self, queryset, name, value):
        textparts_queryset = self.get_lowest_textparts_queryset(value)
        return queryset.filter(tokens__text_part__in=textparts_queryset).distinct()


class NamedEntityNode(DjangoObjectType):
    data = generic.GenericScalar()

    class Meta:
        model = NamedEntity
        interfaces = (relay.Node,)
        filterset_class = NamedEntityFilterSet


class AttributionRecordFilterSet(django_filters.FilterSet):
    reference = django_filters.CharFilter(method="reference_filter")

    class Meta:
        model = AttributionRecord
        fields = []

    def reference_filter(self, queryset, name, value):
        # TODO: Handle path expansion, healed URNs, etc here
        return queryset.filter(data__references__icontains=value)


class AttributionRecordNode(DjangoObjectType):
    name = String()

    class Meta:
        model = AttributionRecord
        interfaces = (relay.Node,)
        filterset_class = AttributionRecordFilterSet

    @classmethod
    def get_queryset(cls, queryset, info):
        return queryset.select_related("person", "organization")


class DictionaryNode(DjangoObjectType):
    # FIXME: Implement access checking for all queries

    class Meta:
        model = Dictionary
        interfaces = (relay.Node,)
        filter_fields = ["urn"]


class DictionaryEntryFilterSet(TextPartsReferenceFilterMixin, django_filters.FilterSet):
    reference = django_filters.CharFilter(method="reference_filter")
    lemma = django_filters.CharFilter(method="lemma_filter")

    class Meta:
        model = DictionaryEntry
        fields = {"urn": ["exact"], "headword": ["exact", "istartswith"]}

    def reference_filter(self, queryset, name, value):
        textparts_queryset = self.get_lowest_textparts_queryset(value)

        if RESOLVE_DICTIONARY_ENTRIES_VIA_LEMMAS:
            # TODO: revisit normalization here with @jtauber
            passage_lemmas = Token.objects.filter(
                text_part__in=textparts_queryset
            ).values_list("lemma", flat=True)
            matches = queryset.filter(headword__in=passage_lemmas)
        # TODO: Determine why graphene bloats the "simple" query;
        # if we just filter the queryset against ids, we're much better off
        elif RESOLVE_CITATIONS_VIA_TEXT_PARTS:
            matches = queryset.filter(
                senses__citations__text_parts__in=textparts_queryset
            )
        else:
            matches = queryset.filter(
                senses__citations__data__urn__in=textparts_queryset.values_list("urn")
            )
        return queryset.filter(pk__in=matches)

    def lemma_filter(self, queryset, name, value):
        value_normalized = normalize_string(value)
        lemma_pattern = (
            rf"^({value_normalized})$|^({value_normalized})[\u002C\u002E\u003B\u00B7\s]"
        )
        return queryset.filter(headword_normalized__regex=lemma_pattern)


def _crush_sense(tree):
    # TODO: Prefer GraphQL Ids
    urn = tree["data"].pop("urn")
    tree["id"] = urn
    tree.pop("data")
    for child in tree.get("children", []):
        _crush_sense(child)


class DictionaryEntryNode(DjangoObjectType):
    data = generic.GenericScalar()
    sense_tree = generic.GenericScalar(
        description="A nested structure returning the URN(s) of senses attached to this entry"
    )

    def resolve_sense_tree(obj, info, **kwargs):
        # TODO: Proper GraphQL field for crushed tree nodes
        data = []
        for sense in obj.senses.filter(depth=1):
            tree = sense.dump_bulk(parent=sense)[0]
            _crush_sense(tree)
            data.append(tree)
        return data

    class Meta:
        model = DictionaryEntry
        interfaces = (relay.Node,)
        filterset_class = DictionaryEntryFilterSet


class SenseFilterSet(TextPartsReferenceFilterMixin, django_filters.FilterSet):
    reference = django_filters.CharFilter(method="reference_filter")

    class Meta:
        model = Sense
        fields = {
            "urn": ["exact", "startswith"],
            "entry": ["exact"],
            "entry__urn": ["exact"],
            "depth": ["exact", "gt", "lt", "gte", "lte"],
            "path": ["exact", "startswith"],
        }

    # TODO: refactor as a mixin
    def reference_filter(self, queryset, name, value):
        textparts_queryset = self.get_lowest_textparts_queryset(value)

        # TODO: Determine why graphene bloats the "simple" query;
        # if we just filter the queryset against ids, we're much better off
        if RESOLVE_CITATIONS_VIA_TEXT_PARTS:
            matches = queryset.filter(citations__text_parts__in=textparts_queryset)
        else:
            matches = queryset.filter(
                citations__data__urn__in=textparts_queryset.values_list("urn")
            )
        return queryset.filter(pk__in=matches)


class SenseNode(DjangoObjectType):
    # TODO: Implement subsenses or descendants either as a top-level
    # field or combining path, depth and URN filters

    class Meta:
        model = Sense
        interfaces = (relay.Node,)
        filterset_class = SenseFilterSet


class CitationFilterSet(TextPartsReferenceFilterMixin, django_filters.FilterSet):
    reference = django_filters.CharFilter(method="reference_filter")

    class Meta:
        model = Citation
        fields = {
            "text_parts__urn": ["exact"],
        }

    # TODO: refactor as a mixin
    def reference_filter(self, queryset, name, value):
        textparts_queryset = self.get_lowest_textparts_queryset(value)
        # TODO: Determine why graphene bloats the "simple" query;
        # if we just filter the queryset against ids, we're much better off
        if RESOLVE_CITATIONS_VIA_TEXT_PARTS:
            matches = queryset.filter(text_parts__in=textparts_queryset).distinct()
        else:
            matches = queryset.filter(
                data__urn__in=textparts_queryset.values_list("urn")
            )
        return queryset.filter(pk__in=matches)


class CitationNode(DjangoObjectType):
    text_parts = LimitedConnectionField(TextPartNode)
    data = generic.GenericScalar()

    ref = String()
    quote = String()
    passage_urn = String()

    def resolve_ref(obj, info, **kwargs):
        return obj.data.get("ref", "")

    def resolve_quote(obj, info, **kwargs):
        return obj.data.get("quote", "")

    def resolve_passage_urn(obj, info, **kwargs):
        # TODO: Do further validation to ensure we can resolve this
        return obj.data.get("urn", "")

    class Meta:
        model = Citation
        interfaces = (relay.Node,)
        filterset_class = CitationFilterSet


class MetadataFilterSet(TextPartsReferenceFilterMixin, django_filters.FilterSet):
    reference = django_filters.CharFilter(method="reference_filter")
    # TODO: Deprecate visible field in favor of visibility
    visible = django_filters.BooleanFilter(method="visible_filter")
    # TODO: Determine why visibility isn't working right, likely related
    # to convert_choices_to_enum being disabled
    visibility = django_filters.CharFilter(method="visibility_filter")

    class Meta:
        model = Metadata
        fields = {
            "collection_urn": ["exact"],
            "value": ["exact"],
            "level": ["exact", "in"],
            "depth": ["exact", "gt", "lt", "gte", "lte"],
        }

    # TODO: Refactor to `Node` or other schema mixins
    def get_workparts_queryset(self, version):
        return version.get_ancestors() | Node.objects.filter(pk=version.pk)

    # TODO: refactor as a mixin
    def reference_filter(self, queryset, name, value):
        textparts_queryset = self.get_lowest_textparts_queryset(value)
        # TODO: Get smarter with an `up_to` filter that could further scope the query

        workparts_queryset = self.get_workparts_queryset(self.request.passage.version)

        union_qs = textparts_queryset | workparts_queryset
        matches = queryset.filter(cts_relations__in=union_qs).distinct()
        return queryset.filter(pk__in=matches)

    def visibility_filter(self, queryset, name, value):
        return queryset.filter(visibility=value)

    def visible_filter(self, queryset, name, value):
        visibility_lookup = {
            True: "reader",
            False: "hidden",
        }
        return queryset.filter(visibility=visibility_lookup[value])


class MetadataNode(DjangoObjectType):
    # NOTE: We are going to specify `PassageTextPartNode` so we can use the reference
    # filter, but it may not be the ideal field long term (mainly, if we want to link to
    # more generic CITE URNs, not just work-part or textpart URNs)
    cts_relations = LimitedConnectionField(lambda: PassageTextPartNode)

    class Meta:
        model = Metadata
        interfaces = (relay.Node,)
        filterset_class = MetadataFilterSet

        # TODO: Resolve with a future update to graphene-django
        convert_choices_to_enum = []


class Query(ObjectType):
    text_group = relay.Node.Field(TextGroupNode)
    text_groups = LimitedConnectionField(TextGroupNode)

    work = relay.Node.Field(WorkNode)
    works = LimitedConnectionField(WorkNode)

    version = relay.Node.Field(VersionNode)
    versions = LimitedConnectionField(VersionNode)

    text_part = relay.Node.Field(TextPartNode)
    text_parts = LimitedConnectionField(TextPartNode)

    # No passage_text_part endpoint available here like the others because we
    # will only support querying by reference.
    passage_text_parts = LimitedConnectionField(PassageTextPartNode)

    text_alignment = relay.Node.Field(TextAlignmentNode)
    text_alignments = LimitedConnectionField(TextAlignmentNode)

    text_alignment_record = relay.Node.Field(TextAlignmentRecordNode)
    text_alignment_records = LimitedConnectionField(TextAlignmentRecordNode)

    text_alignment_record_relation = relay.Node.Field(TextAlignmentRecordRelationNode)
    text_alignment_record_relations = LimitedConnectionField(
        TextAlignmentRecordRelationNode
    )

    text_annotation = relay.Node.Field(TextAnnotationNode)
    text_annotations = LimitedConnectionField(TextAnnotationNode)

    syntax_tree = relay.Node.Field(SyntaxTreeNode)
    syntax_trees = LimitedConnectionField(SyntaxTreeNode)

    metrical_annotation = relay.Node.Field(MetricalAnnotationNode)
    metrical_annotations = LimitedConnectionField(MetricalAnnotationNode)

    image_annotation = relay.Node.Field(ImageAnnotationNode)
    image_annotations = LimitedConnectionField(ImageAnnotationNode)

    audio_annotation = relay.Node.Field(AudioAnnotationNode)
    audio_annotations = LimitedConnectionField(AudioAnnotationNode)

    tree = Field(TreeNode, urn=String(required=True), up_to=String(required=False))

    token = relay.Node.Field(TokenNode)
    tokens = LimitedConnectionField(TokenNode)

    named_entity = relay.Node.Field(NamedEntityNode)
    named_entities = LimitedConnectionField(NamedEntityNode)

    repo = relay.Node.Field(RepoNode)
    repos = LimitedConnectionField(RepoNode)

    attribution = relay.Node.Field(AttributionRecordNode)
    attributions = LimitedConnectionField(AttributionRecordNode)

    dictionary = relay.Node.Field(DictionaryNode)
    dictionaries = LimitedConnectionField(DictionaryNode)

    dictionary_entry = relay.Node.Field(DictionaryEntryNode)
    dictionary_entries = LimitedConnectionField(DictionaryEntryNode)

    sense = relay.Node.Field(SenseNode)
    senses = LimitedConnectionField(SenseNode)

    citation = relay.Node.Field(CitationNode)
    citations = LimitedConnectionField(CitationNode)

    metadata_record = relay.Node.Field(MetadataNode)
    metadata_records = LimitedConnectionField(MetadataNode)

    def resolve_tree(obj, info, urn, **kwargs):
        return TextPart.dump_tree(
            root=TextPart.objects.get(urn=urn), up_to=kwargs.get("up_to")
        )
