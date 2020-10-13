# Generated by Django 2.2.15 on 2020-09-11 16:50

import django.db.models.deletion
from django.db import migrations, models

import django_jsonfield_backport.models
import sortedm2m.fields


class Migration(migrations.Migration):

    dependencies = [
        ("scaife_viewer_atlas", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="TextAlignmentRecord",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("urn", models.CharField(max_length=255, unique=True)),
                (
                    "metadata",
                    django_jsonfield_backport.models.JSONField(
                        blank=True, default=dict
                    ),
                ),
                ("idx", models.IntegerField(help_text="0-based index")),
            ],
            options={"ordering": ["idx"],},
        ),
        migrations.CreateModel(
            name="TextAlignmentRecordRelation",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "record",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="relations",
                        to="scaife_viewer_atlas.TextAlignmentRecord",
                    ),
                ),
                (
                    "tokens",
                    models.ManyToManyField(
                        related_name="alignment_record_relations",
                        to="scaife_viewer_atlas.Token",
                    ),
                ),
                (
                    "version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="scaife_viewer_atlas.Node",
                    ),
                ),
            ],
        ),
        migrations.RenameField(
            model_name="textalignment", old_name="name", new_name="label",
        ),
        migrations.RemoveField(model_name="textalignment", name="slug",),
        migrations.RemoveField(model_name="textalignment", name="version",),
        migrations.AddField(
            model_name="textalignment",
            name="description",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="textalignment",
            name="urn",
            field=models.CharField(default="", max_length=255, unique=True),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="textalignment",
            name="versions",
            field=sortedm2m.fields.SortedManyToManyField(
                help_text=None,
                related_name="text_alignments",
                to="scaife_viewer_atlas.Node",
            ),
        ),
        migrations.DeleteModel(name="TextAlignmentChunk",),
        migrations.AddField(
            model_name="textalignmentrecord",
            name="alignment",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="records",
                to="scaife_viewer_atlas.TextAlignment",
            ),
        ),
    ]
