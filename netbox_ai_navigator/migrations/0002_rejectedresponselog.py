import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("netbox_ai_navigator", "0001_add_navigator_permissions"),
    ]

    operations = [
        migrations.CreateModel(
            name="RejectedResponseLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("username", models.CharField(max_length=255, verbose_name="Username")),
                ("user_request", models.TextField(verbose_name="Last request")),
                ("rejected_response", models.TextField(verbose_name="Rejected model response")),
                ("delivered_response", models.TextField(verbose_name="Delivered response")),
                (
                    "reason",
                    models.CharField(
                        choices=[
                            ("scope_guard", "Outside NetBox scope"),
                            ("change_guard", "Change response was not validated"),
                            ("approval_normalization", "Change approval response was replaced"),
                            ("proposal_guard", "Unsafe change proposal response"),
                            ("grounding_guard", "Response could not be grounded in NetBox data"),
                        ],
                        max_length=32,
                        verbose_name="Reason",
                    ),
                ),
                ("provider", models.CharField(blank=True, max_length=50, verbose_name="Model provider")),
                ("model_name", models.CharField(blank=True, max_length=255, verbose_name="Model name")),
                ("created", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Created")),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="User",
                    ),
                ),
            ],
            options={
                "verbose_name": "rejected AI response",
                "verbose_name_plural": "rejected AI responses",
                "ordering": ("-created", "-pk"),
                "default_permissions": ("view",),
            },
        ),
    ]
