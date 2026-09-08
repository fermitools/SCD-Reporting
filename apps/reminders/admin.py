from django.contrib import admin

from .models import ReminderLog, ReminderSchedule, ReminderTemplate


@admin.register(ReminderTemplate)
class ReminderTemplateAdmin(admin.ModelAdmin):
    list_display = ('name', 'subject', 'is_default', 'updated_by', 'updated_at')
    list_filter = ('is_default',)
    search_fields = ('name', 'subject', 'body')


@admin.register(ReminderSchedule)
class ReminderScheduleAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'template', 'cadence_display', 'is_enabled',
                    'next_run_at', 'last_run_at')
    list_filter = ('is_enabled', 'frequency')
    search_fields = ('name', 'owner__email')
    readonly_fields = ('last_run_at',)


@admin.register(ReminderLog)
class ReminderLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'to_email', 'status', 'schedule', 'sent_by', 'last_entry_at')
    list_filter = ('status',)
    search_fields = ('to_email', 'subject', 'detail')
    readonly_fields = tuple(f.name for f in ReminderLog._meta.fields)
