from django.db import migrations, models


def classify_existing_logs(apps, _schema_editor):
    response_log = apps.get_model("netbox_ai_navigator", "RejectedResponseLog")
    response_log.objects.filter(reason="approval_normalization").update(category="write")


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_ai_navigator", "0002_rejectedresponselog"),
    ]

    operations = [
        migrations.AddField(
            model_name="rejectedresponselog",
            name="category",
            field=models.CharField(
                choices=[("rejected", "Rejected"), ("write", "Write operation")],
                db_index=True,
                default="rejected",
                max_length=16,
                verbose_name="Category",
            ),
        ),
        migrations.RunPython(classify_existing_logs, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="rejectedresponselog",
            name="reason",
            field=models.CharField(
                choices=[
                    ("scope_guard", "Outside NetBox scope"),
                    ("change_guard", "Change response was not validated"),
                    ("approval_normalization", "Change proposal validated"),
                    ("proposal_guard", "Unsafe change proposal response"),
                    ("grounding_guard", "Response could not be grounded in NetBox data"),
                ],
                max_length=32,
                verbose_name="Reason",
            ),
        ),
        migrations.AlterField(
            model_name="rejectedresponselog",
            name="rejected_response",
            field=models.TextField(verbose_name="Model response"),
        ),
        migrations.AlterModelOptions(
            name="rejectedresponselog",
            options={
                "default_permissions": ("view",),
                "ordering": ("-created", "-pk"),
                "verbose_name": "AI response log",
                "verbose_name_plural": "AI response logs",
            },
        ),
    ]
