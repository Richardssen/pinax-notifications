import datetime

from django.db import models
from django.conf import settings
from django.db.models import Q
from django.db.models import get_model
from django.core.urlresolvers import reverse
from django.template.loader import render_to_string

from django.contrib.sites.models import Site
from django.contrib.auth.models import User

from django.utils.translation import ugettext_lazy as _

# favour django-mailer but fall back to django.core.mail
try:
    from mailer import send_mail
except ImportError:
    from django.core.mail import send_mail


class NoticeType(models.Model):
    
    label = models.CharField(_('label'), max_length=20)
    display = models.CharField(_('display'), max_length=50)
    description = models.CharField(_('description'), max_length=100)
    
    def __unicode__(self):
        return self.label
    
    class Admin:
        list_display = ('label', 'display', 'description')
    
    class Meta:
        verbose_name = _("notice type")
        verbose_name_plural = _("notice types")

# if this gets updated, the create() method below needs to be as well...
NOTICE_MEDIA = (
    ("1", _("Email")),
)

class NoticeSetting(models.Model):
    """
    Indicates, for a given user, whether to send notifications
    of a given type to a given medium.
    """
    
    user = models.ForeignKey(User, verbose_name=_('user'))
    notice_type = models.ForeignKey(NoticeType, verbose_name=_('notice type'))
    medium = models.CharField(_('medium'), max_length=1, choices=NOTICE_MEDIA)
    send = models.BooleanField(_('send'), default=True)
    
    class Admin:
        list_display = ('id', 'user', 'notice_type', 'medium', 'send')
    
    class Meta:
        verbose_name = _("notice setting")
        verbose_name_plural = _("notice settings")

def should_send(user, notice_type, medium, default):
    try:
        return NoticeSetting.objects.get(user=user, notice_type=notice_type, medium=medium).send
    except NoticeSetting.DoesNotExist:
        NoticeSetting(user=user, notice_type=notice_type, medium=medium, send=default).save()
        return default


class Notice(models.Model):
    
    user = models.ForeignKey(User, verbose_name=_('user'))
    message = models.TextField(_('message'))
    notice_type = models.ForeignKey(NoticeType, verbose_name=_('notice type'))
    added = models.DateTimeField(_('added'), default=datetime.datetime.now)
    unseen = models.BooleanField(_('unseen'), default=True)
    archived = models.BooleanField(_('archived'), default=False)
    
    def __unicode__(self):
        return self.message
    
    def archive(self):
        self.archived = True
        self.save()
    
    def is_unseen(self):
        """
        returns value of self.unseen but also changes it to false.
        
        Use this in a template to mark an unseen notice differently the first
        time it is shown.
        """
        unseen = self.unseen
        if unseen:
            self.unseen = False
            self.save()
        return unseen
    
    def html_message(self):
        return message_to_html(self.message)
    
    class Meta:
        ordering = ["-added"]
        verbose_name = _("notice")
        verbose_name_plural = _("notices")
    
    class Admin:
        list_display = ('message', 'user', 'notice_type', 'added', 'unseen', 'archived')


def create_notice_type(label, display, description):
    """
    create a new NoticeType.
    
    This is intended to be used by other apps as a post_syncdb manangement step.
    """
    try:
        notice_type = NoticeType.objects.get(label=label)
        updated = False
        if display != notice_type.display:
            notice_type.display = display
            updated = True
        if description != notice_type.description:
            notice_type.description = description
            updated = True
        if updated:
            notice_type.save()
            print "Updated %s NoticeType" % label
    except NoticeType.DoesNotExist:
        NoticeType(label=label, display=display, description=description).save()
        print "Created %s NoticeType" % label


def encode_object(obj):
    return "{%s.%s.%s}" % (obj._meta.app_label, obj._meta.object_name, obj.pk)


def encode_message(message_template, *objects):
    return message_template % tuple(encode_object(obj) for obj in objects)

def decode_object(ref):
    app, name, pk = ref.split(".")
    return get_model(app, name).objects.get(pk=pk)

class FormatException(Exception):
    pass

def decode_message(message, decoder):
    out = []
    in_field = False
    prev = 0
    for index, ch in enumerate(message):
        if not in_field:
            if ch == '{':
                in_field = True
                if prev != index:
                    out.append(message[prev:index])
                prev = index
            elif ch == '}':
                raise FormatException("unmatched }")
        elif in_field:
            if ch == '{':
                raise FormatException("{ inside {}")
            elif ch == '}':
                in_field = False
                out.append(decoder(message[prev+1:index]))
                prev = index + 1
    if in_field:
        raise FormatException("unmatched {")
    if prev <= index:
        out.append(message[prev:index+1])
    return "".join(out)

def message_to_text(message):
    def decoder(ref):
        return unicode(decode_object(ref))
    return decode_message(message, decoder)

def message_to_html(message):
    def decoder(ref):
        obj = decode_object(ref)
        return u"""<a href="%s">%s</a>""" % (obj.get_absolute_url(), unicode(obj))
    return decode_message(message, decoder)


def send(users, notice_type_label, message_template, object_list=[], issue_notice=True):
    """
    create a new notice.
    
    This is intended to be how other apps create new notices.
    """
    notice_type = NoticeType.objects.get(label=notice_type_label)
    message = encode_message(message_template, *object_list)
    recipients = []
    
    notices_url = u"http://%s%s" % (
        unicode(Site.objects.get_current()),
        reverse("notification_notices"),
    )
    
    subject = render_to_string("notification/notification_subject.txt", {
        "display": notice_type.display,
    })
    message_body = render_to_string("notification/notification_body.txt", {
        "message": message_to_text(message),
        "notices_url": notices_url,
    })

    for user in users:
        if issue_notice:
            notice = Notice(user=user, message=message, notice_type=notice_type)
            notice.save()
        if should_send(user, notice_type, "1", default=True) and user.email: # Email
            recipients.append(user.email)
    send_mail(subject, message_body, settings.DEFAULT_FROM_EMAIL, recipients)


def notices_for(user, archived=False):
    """
    returns Notice objects for the given user.
    
    If archived=False, it only include notices not archived.
    If archived=True, it returns all notices for that user.
    Superusers receive all notices.
    """
    if user.is_superuser:
        q = Q()
    else:
        q = Q(user=user)
    if archived:
        return Notice.objects.filter(q)
    else:
        return Notice.objects.filter(q, archived=archived)


def unseen_count_for(user):
    """
    returns the number of unseen notices for the given user but does not
    mark them seen
    """
    return Notice.objects.filter(user=user, unseen=True).count()