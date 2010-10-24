from django.db import models
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes import generic
from django.utils.translation import ugettext_lazy as _
from django.core.urlresolvers import reverse
from django.utils import translation
from django.core import mail
from django.conf import settings
from django.template import loader, Context, Template, RequestContext
from django.contrib.sites.models import Site
from django.db.models import signals
from django.contrib import admin
from django.shortcuts import get_object_or_404
from django.http import HttpResponseRedirect, HttpRequest
from django.core.validators import email_re
from django.template.loader import render_to_string
from django.core.mail.utils import DNS_NAME


from feincms.models import Base
from feincms.management.checker import check_database_schema
from feincms.utils import copy_model_instance

from Mailman.Bouncers.BouncerAPI import ScanMessages

import exceptions
import hashlib
import random
import sys
import datetime
import spf
import socket
import poplib
import email

      
class Newsletter(Base):
    """A newsletter with subject and content
    can contain multiple jobs with mails to send"""
    name = models.CharField(verbose_name="Name", help_text="Wird nur intern verwendet.", max_length=100)
    active = models.BooleanField(default=True)
    sender = models.ForeignKey('Sender', verbose_name="Absender")
    subject = models.CharField(verbose_name="Betreff", max_length=250)
    reply_email = models.EmailField(verbose_name="Reply-to" ,blank=True)
    language = models.CharField(max_length=6, verbose_name="Sprache", choices=settings.LANGUAGES)
    
    #ga tracking
    utm_source = models.SlugField(verbose_name="Utm Source", default="newsletter")
    utm_medium = models.SlugField(verbose_name="Utm Medium", default="cpc")

    class Meta:
        ordering = ('subject',)
        verbose_name = "Newsletter"
        verbose_name_plural = "Newsletters"
            
    def __unicode__(self):
        return self.name
    
    def is_valid(self):
        if self.subject == '':
            return False
        # todo: check if email is valid
        return True
    
    def create_snapshot(self):
        snapshot = copy_model_instance(self, exclude=('id',))
        snapshot.active = False
        snapshot.save()
        snapshot.copy_content_from(self)
        return snapshot
        
    def replace_links(self, job):
        """
        Searches al links in content sections
        """
        for cls in self._feincms_content_types:
            for content in cls.objects.filter(parent=self):
                content.replace_links(job)
                content.save()
    
signals.post_syncdb.connect(check_database_schema(Newsletter, __name__), weak=False)

class NewsletterReceiverMixin(object):
    """
    Abstract baseclass for every object that can receive a newsletter
    """    
    def get_email(self):
        if hasattr(self,'email'):
            return self.email
        raise exceptions.NotImplementedError('Need a get_email implementation.')

class NewsletterJobUnitMixin(object):
    """
    Abstract baseclass for every object which can be target of a NewsletterJob
    """    
    def create_newsletter(self):
        """
        Creates a newsletter for every NewsletterReceiverMixin
        """
        job = NewsletterJob(group_object=self)
        job.save()
        job.create_mails()
        return job
    
    def get_newsletter_receivers(self):
        """
        Tries to get a queryset named client or participants bevore giving up.
        """
        queryset = getattr(self, 'clients', None)
        if queryset:
            return queryset
        queryset = getattr(self, 'participants', None)
        if queryset:
            return queryset
        raise exeptions.NotImplementedError("Didn't find any subset, you need to implement get_newsletter_receivers yourselfe.")

class NewsletterJobUnitAdmin(admin.ModelAdmin):
    change_form_template = "admin/pennyblack/jobunit/change_form.html"
    
    def create_newsletter(self, request, object_id):
        from django.shortcuts import get_object_or_404
        obj = get_object_or_404(self.model, pk=object_id)
        job = obj.create_newsletter()
        return HttpResponseRedirect(reverse('admin:pennyblack_newsletterjob_change', args=(job.id,)))
    
    def get_urls(self):
        from django.conf.urls.defaults import patterns
        urls = super(NewsletterJobUnitAdmin, self).get_urls()
        my_urls = patterns('',
            (r'^(?P<object_id>\d+)/create_newsletter/$', self.admin_site.admin_view(self.create_newsletter))
        )
        return my_urls + urls
        


class NewsletterJob(models.Model):
    """A bunch of participants wich receive a newsletter"""
    newsletter = models.ForeignKey(Newsletter, related_name="jobs", null=True, limit_choices_to = {'active': True})
    status = models.IntegerField(choices=((1,'Draft'),(2,'Pending'),(3,'Sending'),(4,'Finished'),(5,'Error'),), default=1)
    date_created = models.DateTimeField(verbose_name="Created", default=datetime.datetime.now())
    date_deliver_start = models.DateTimeField(blank=True, null=True, verbose_name="Delivering Started", default=None)
    date_deliver_finished = models.DateTimeField(blank=True, null=True, verbose_name="Delivering Finished", default=None)

    content_type = models.ForeignKey(ContentType)
    object_id = models.PositiveIntegerField()
    group_object = generic.GenericForeignKey('content_type', 'object_id')
    
    #ga tracking
    utm_campaign = models.SlugField(verbose_name="Utm Campaign")
    
    
    class Meta:
        ordering = ('date_created',)
        verbose_name = "Newsletter delivery task"
        verbose_name_plural = "Newsletter delivery tasks"
        
    def __unicode__(self):
        return (self.newsletter.subject if self.newsletter is not None else "unasigned NewsletterJob")
    
    def delete(self, *args, **kwargs):
        if self.newsletter.active == False:
            self.newsletter.delete()
        super(NewsletterJob, self).delete(*args, **kwargs)
    
    def total_mails(self):
        return str(self.mails.count())
    total_mails.short_description = '# of mails'
    
    def delivery_status(self):
        return str(self.mails.filter(sent=True).count())
    delivery_status.short_description = '# of mails sent'
    
    def viewed(self):
        return str(self.mails.filter(viewed=True).count())
    viewed.short_description = '# of views'
    
    def can_send(self):
        if self.status != 1 and self.status != 5:
            return False
        return self.is_valid()

    def is_valid(self):
        if self.newsletter == None or not self.newsletter.is_valid():
            return False
        return True
    
    def create_mails(self):
        """
        Create mails for every NewsletterReceiverMixin in self.group_object.
        """
        if not hasattr(self.group_object, 'get_newsletter_receivers'):
            raise exceptions.NotImplementedError('Object needs to implement get_newsletter_receivers')
        for receiver in self.group_object.get_newsletter_receivers():
            self.mails.add(Mail(person=receiver))
    
    def add_link(self, link):
        """
        Adds a link and returns a replacement link
        """
        link = Link(link_target=link, job=self)
        link.save()
        return "http://" + Site.objects.all()[0].domain + reverse('pennyblack.redirect_link', kwargs={'mail_hash':'{{person.mail_hash}}','link_hash':link.link_hash})
    
    def send(self):
        if not self.can_send():
            raise exceptions.Exception('This job is not valid')
        self.newsletter = self.newsletter.create_snapshot()
        self.newsletter.replace_links(self)
        self.status = 3
        self.date_deliver_start = datetime.datetime.now()
        self.save()
        try:
            translation.activate(self.newsletter.language)
            connection = mail.get_connection()
            connection.open()
            for newsletter_mail in self.mails.filter(sent=False):
                connection.send_messages([newsletter_mail.get_message()])
                newsletter_mail.mark_sent()
            connection.close()
        except:
            self.status = 5
            raise
        else:
            self.status = 4
            self.date_deliver_finished = datetime.datetime.now()
        self.save()
        

class Link(models.Model):
    job = models.ForeignKey(NewsletterJob)
    link_hash = models.CharField(max_length=32, blank=True)
    link_target = models.CharField(verbose_name="Adresse", max_length=500)
    click_count = models.IntegerField(default=0)

    def __unicode__(self):
        return self.link_target

    def save(self, **kwargs):
        if self.link_hash == u'':
            self.link_hash = hashlib.md5(str(self.id)+str(random.random())).hexdigest()
        super(Link, self).save(**kwargs)

class Mail(models.Model):
    """
    This is a single Mail, it's part of a NewsletterJob
    """
    viewed = models.BooleanField(default=False)
    bounced = models.BooleanField(default=False)
    sent = models.BooleanField(default=False)
    content_type = models.ForeignKey(ContentType)
    object_id = models.PositiveIntegerField()
    person = generic.GenericForeignKey('content_type', 'object_id')
    job = models.ForeignKey(NewsletterJob, related_name="mails")
    mail_hash = models.CharField(max_length=32, blank=True)
    
    def __unicode__(self):
        return '%s to %s' % (self.job, self.person,)
        
    def save(self, **kwargs):
        if self.mail_hash == u'':
            self.mail_hash = hashlib.md5(str(self.id)+str(random.random())).hexdigest()
        super(Mail, self).save(**kwargs)
    
    def mark_sent(self):
        self.sent = True
        self.save()
    
    def is_valid(self):
        """
        Checks if this Mail is valid
        """
        return email_re.match(self.person.get_email())

    def get_email(self):
        return self.person.get_email()
    get_email.short_description = "E-Mail"

    def get_message(self):
        newsletter = self.job.newsletter
        context = self.get_context()
        context['newsletter'] = newsletter
        content = render_to_string(newsletter.template.path,
            context, context_instance=RequestContext(HttpRequest()))
        
        if self.job.newsletter.reply_email!='':
            headers={'Reply-To': self.job.newsletter.reply_email}
        else:
            headers={}
        message = mail.EmailMessage(
            self.job.newsletter.subject,
            content,
            self.job.newsletter.sender.email,
            [self.person.email],
            headers=headers,
        )
        message.content_subtype = "html"
        return message
    
    def get_context(self):
        pingback_url = "http://" + Site.objects.all()[0].domain + reverse('pennyblack.ping', args=[self.mail_hash,'',])

        weblink = _("To view this email as a web page, click [here]")
        url = "http://" + Site.objects.all()[0].domain + reverse('pennyblack.view', args=[self.mail_hash])
        weblink = weblink.replace("[",'<a href="'+url+'">').replace("]",'</a>')
        
        return {
            # todo: newsletter url konzept aendern
            'NEWSLETTER_URL': 'asdf',#settings.NEWSLETTER_URL,
            'person': self.person,
            'group_object': self.job.group_object,
            'pingback_url': pingback_url,
            'weblink':weblink,
        }

class Sender(models.Model):
    email = models.EmailField(verbose_name="Von E-Mail Adresse")
    name = models.CharField(verbose_name="Von Name", help_text="Wird in vielen E-Mail Clients als Von angezeit.", max_length=100)
    pop_username = models.CharField(verbose_name="Pop3 Username", max_length=100, blank=True)
    pop_password = models.CharField(verbose_name="Pop3 Passwort", max_length=100, blank=True)
    pop_server = models.CharField(verbose_name="Pop3 Server", max_length=100, blank=True)
    pop_port = models.IntegerField(verbose_name="Pop3 Port", max_length=100, default=110)
    
    def __unicode__(self):
        return self.email
    
    def check_spf(self):
        """
        Check if sender is authorised by sender policy framework
        """
        return spf.check(i=socket.gethostbyname(DNS_NAME.get_fqdn()),s=self.email,h=DNS_NAME.get_fqdm())
    
    def spf_result(self):
        return self.check_spf()
    check_spf.short_description = "spf Result"
    
    def get_mail(self):
        conn = poplib.POP3(self.pop_server, self.pop_port)
        conn.user(self.pop_username)
        conn.pass_(self.pop_password)
        (numMessages, totalSize) = conn.stat()
        print 'messages '+str(numMessages)
        print 'size '+str(totalSize)
        for i in range(1,numMessages+1):
            (comment, lines, octets) = conn.retr(i)
            lines.append('')
            data = '\n'.join(lines)
            mime = email.message_from_string(data)
            print ScanMessages(None, mime)
        conn.quit()
        
    