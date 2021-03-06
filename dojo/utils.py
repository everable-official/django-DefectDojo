import re
import binascii
import os
import hashlib
import json
import io
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from calendar import monthrange
from datetime import date, datetime
from math import pi, sqrt
import vobject
import requests
from dateutil.relativedelta import relativedelta, MO, SU
from django.conf import settings
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.urls import get_resolver, reverse
from django.db.models import Q, Sum, Case, When, IntegerField, Value, Count
from django.template.defaultfilters import pluralize
from django.template.loader import render_to_string
from django.utils import timezone
from jira import JIRA
from jira.exceptions import JIRAError
from django.dispatch import receiver
from dojo.signals import dedupe_signal
from django.db.models.signals import post_save
import calendar as tcalendar
from dojo.github import add_external_issue_github, update_external_issue_github, close_external_issue_github, reopen_external_issue_github
from dojo.models import Finding, Engagement, Finding_Template, Product, JIRA_PKey, JIRA_Issue,\
    Dojo_User, User, System_Settings, Notifications, Endpoint, Benchmark_Type, \
    Language_Type, Languages, Rule, Test_Type
from asteval import Interpreter
from requests.auth import HTTPBasicAuth
from dojo.notifications.helper import create_notification
import logging
import itertools
from django.contrib import messages
from django.http import HttpResponseRedirect


logger = logging.getLogger(__name__)
deduplicationLogger = logging.getLogger("dojo.specific-loggers.deduplication")


"""
Helper functions for DefectDojo
"""


def sync_false_history(new_finding, *args, **kwargs):
    if new_finding.endpoints.count() == 0:
        eng_findings_cwe = Finding.objects.filter(
            test__engagement__product=new_finding.test.engagement.product,
            cwe=new_finding.cwe,
            test__test_type=new_finding.test.test_type,
            false_p=True, hash_code=new_finding.hash_code).exclude(id=new_finding.id).exclude(cwe=None)
        eng_findings_title = Finding.objects.filter(
            test__engagement__product=new_finding.test.engagement.product,
            title=new_finding.title,
            test__test_type=new_finding.test.test_type,
            false_p=True, hash_code=new_finding.hash_code).exclude(id=new_finding.id)
        total_findings = eng_findings_cwe | eng_findings_title
    else:
        eng_findings_cwe = Finding.objects.filter(
            test__engagement__product=new_finding.test.engagement.product,
            cwe=new_finding.cwe,
            test__test_type=new_finding.test.test_type,
            false_p=True).exclude(id=new_finding.id).exclude(cwe=None).exclude(endpoints=None)
        eng_findings_title = Finding.objects.filter(
            test__engagement__product=new_finding.test.engagement.product,
            title=new_finding.title,
            test__test_type=new_finding.test.test_type,
            false_p=True).exclude(id=new_finding.id).exclude(endpoints=None)
    total_findings = eng_findings_cwe | eng_findings_title
    if total_findings.count() > 0:
        new_finding.false_p = True
        new_finding.active = False
        new_finding.verified = True
        super(Finding, new_finding).save(*args, **kwargs)


# true if both findings are on an engagement that have a different "deduplication on engagement" configuration
def is_deduplication_on_engagement_mismatch(new_finding, to_duplicate_finding):
    return not new_finding.test.engagement.deduplication_on_engagement and to_duplicate_finding.test.engagement.deduplication_on_engagement


@receiver(dedupe_signal, sender=Finding)
def sync_dedupe(sender, *args, **kwargs):
    try:
        enabled = System_Settings.objects.get().enable_deduplication
    except System_Settings.DoesNotExist:
        enabled = False
    if enabled:
        new_finding = kwargs['new_finding']
        deduplicationLogger.debug('sync_dedupe for: ' + str(new_finding.id) +
                    ":" + str(new_finding.title))
        if hasattr(settings, 'DEDUPLICATION_ALGORITHM_PER_PARSER'):
            scan_type = new_finding.test.test_type.name
            deduplicationLogger.debug('scan_type for this finding is :' + scan_type)
            # Default algorithm
            deduplicationAlgorithm = settings.DEDUPE_ALGO_LEGACY
            # Check for an override for this scan_type in the deduplication configuration
            if (scan_type in settings.DEDUPLICATION_ALGORITHM_PER_PARSER):
                deduplicationAlgorithm = settings.DEDUPLICATION_ALGORITHM_PER_PARSER[scan_type]
            deduplicationLogger.debug('deduplication algorithm: ' + deduplicationAlgorithm)
            if(deduplicationAlgorithm == settings.DEDUPE_ALGO_UNIQUE_ID_FROM_TOOL):
                deduplicate_unique_id_from_tool(new_finding)
            elif(deduplicationAlgorithm == settings.DEDUPE_ALGO_HASH_CODE):
                deduplicate_hash_code(new_finding)
            elif(deduplicationAlgorithm == settings.DEDUPE_ALGO_UNIQUE_ID_FROM_TOOL_OR_HASH_CODE):
                deduplicate_uid_or_hash_code(new_finding)
            else:
                deduplicate_legacy(new_finding)
        else:
            deduplicationLogger.debug("no configuration per parser found; using legacy algorithm")
            deduplicate_legacy(new_finding)
    else:
        deduplicationLogger.debug("skipping dedupe because it's disabled in system settings get()")


def deduplicate_legacy(new_finding):
    # ---------------------------------------------------------
    # 1) Collects all the findings that have the same:
    #      (title  and static_finding and dynamic_finding)
    #      or (CWE and static_finding and dynamic_finding)
    #    as the new one
    #    (this is "cond1")
    # ---------------------------------------------------------
    if new_finding.test.engagement.deduplication_on_engagement:
        eng_findings_cwe = Finding.objects.filter(
            test__engagement=new_finding.test.engagement,
            cwe=new_finding.cwe).exclude(id=new_finding.id).exclude(cwe=0).exclude(duplicate=True)
        eng_findings_title = Finding.objects.filter(
            test__engagement=new_finding.test.engagement,
            title=new_finding.title).exclude(id=new_finding.id).exclude(duplicate=True)
    else:
        eng_findings_cwe = Finding.objects.filter(
            test__engagement__product=new_finding.test.engagement.product,
            cwe=new_finding.cwe).exclude(id=new_finding.id).exclude(cwe=0).exclude(duplicate=True)
        eng_findings_title = Finding.objects.filter(
            test__engagement__product=new_finding.test.engagement.product,
            title=new_finding.title).exclude(id=new_finding.id).exclude(duplicate=True)

    total_findings = eng_findings_cwe | eng_findings_title
    deduplicationLogger.debug("Found " +
        str(len(eng_findings_cwe)) + " findings with same cwe, " +
        str(len(eng_findings_title)) + " findings with same title: " +
        str(len(total_findings)) + " findings with either same title or same cwe")

    # total_findings = total_findings.order_by('date')
    for find in total_findings:
        flag_endpoints = False
        flag_line_path = False
        flag_hash = False
        if is_deduplication_on_engagement_mismatch(new_finding, find):
            deduplicationLogger.debug(
                'deduplication_on_engagement_mismatch, skipping dedupe.')
            continue
        # ---------------------------------------------------------
        # 2) If existing and new findings have endpoints: compare them all
        #    Else look at line+file_path
        #    (if new finding is not static, do not deduplicate)
        # ---------------------------------------------------------
        if find.endpoints.count() != 0 and new_finding.endpoints.count() != 0:
            list1 = [e.host_with_port for e in new_finding.endpoints.all()]
            list2 = [e.host_with_port for e in find.endpoints.all()]
            if all(x in list1 for x in list2):
                flag_endpoints = True
        elif new_finding.static_finding and len(new_finding.file_path) > 0:
            if str(find.line) == str(new_finding.line) and find.file_path == new_finding.file_path:
                flag_line_path = True
            else:
                deduplicationLogger.debug("no endpoints on one of the findings and file_path doesn't match")
        else:
            deduplicationLogger.debug("no endpoints on one of the findings and the new finding is either dynamic or doesn't have a file_path; Deduplication will not occur")
        if find.hash_code == new_finding.hash_code:
            flag_hash = True
        deduplicationLogger.debug(
            'deduplication flags for new finding ' + str(new_finding.id) + ' and existing finding ' + str(find.id) +
            ' flag_endpoints: ' + str(flag_endpoints) + ' flag_line_path:' + str(flag_line_path) + ' flag_hash:' + str(flag_hash))
        # ---------------------------------------------------------
        # 3) Findings are duplicate if (cond1 is true) and they have the same:
        #    hash
        #    and (endpoints or (line and file_path)
        # ---------------------------------------------------------
        if ((flag_endpoints or flag_line_path) and flag_hash):
            try:
                set_duplicate(new_finding, find)
            except Exception as e:
                deduplicationLogger.debug(str(e))
                continue

            break


def deduplicate_unique_id_from_tool(new_finding):
    if new_finding.test.engagement.deduplication_on_engagement:
        existing_findings = Finding.objects.filter(
            test__engagement=new_finding.test.engagement,
            unique_id_from_tool=new_finding.unique_id_from_tool).exclude(
                id=new_finding.id).exclude(
                    unique_id_from_tool=None).exclude(
                        duplicate=True)
    else:
        existing_findings = Finding.objects.filter(
            test__engagement__product=new_finding.test.engagement.product,
            # the unique_id_from_tool is unique for a given tool: do not compare with other tools
            test__test_type=new_finding.test.test_type,
            unique_id_from_tool=new_finding.unique_id_from_tool).exclude(
                id=new_finding.id).exclude(
                    unique_id_from_tool=None).exclude(
                        duplicate=True)
    deduplicationLogger.debug("Found " +
        str(len(existing_findings)) + " findings with same unique_id_from_tool")
    for find in existing_findings:
        if is_deduplication_on_engagement_mismatch(new_finding, find):
            deduplicationLogger.debug(
                'deduplication_on_engagement_mismatch, skipping dedupe.')
            continue
        try:
            set_duplicate(new_finding, find)
        except Exception as e:
            deduplicationLogger.debug(str(e))
            continue
        break


def deduplicate_hash_code(new_finding):
    if new_finding.test.engagement.deduplication_on_engagement:
        existing_findings = Finding.objects.filter(
            test__engagement=new_finding.test.engagement,
            hash_code=new_finding.hash_code).exclude(
                id=new_finding.id).exclude(
                    hash_code=None).exclude(
                        duplicate=True)
    else:
        existing_findings = Finding.objects.filter(
            test__engagement__product=new_finding.test.engagement.product,
            hash_code=new_finding.hash_code).exclude(
                id=new_finding.id).exclude(
                    hash_code=None).exclude(
                        duplicate=True)
    deduplicationLogger.debug("Found " +
        str(len(existing_findings)) + " findings with same hash_code")
    for find in existing_findings:
        if is_deduplication_on_engagement_mismatch(new_finding, find):
            deduplicationLogger.debug(
                'deduplication_on_engagement_mismatch, skipping dedupe.')
            continue
        try:
            set_duplicate(new_finding, find)
        except Exception as e:
            deduplicationLogger.debug(str(e))
            continue
        break


def deduplicate_uid_or_hash_code(new_finding):
    if new_finding.test.engagement.deduplication_on_engagement:
        existing_findings = Finding.objects.filter(
            Q(hash_code=new_finding.hash_code) |
            (Q(unique_id_from_tool=new_finding.unique_id_from_tool) & Q(test__test_type=new_finding.test.test_type)),
            test__engagement=new_finding.test.engagement).exclude(
                id=new_finding.id).exclude(
                    hash_code=None).exclude(
                        duplicate=True)
    else:
        existing_findings = Finding.objects.filter(
            Q(hash_code=new_finding.hash_code) |
            (Q(unique_id_from_tool=new_finding.unique_id_from_tool) & Q(test__test_type=new_finding.test.test_type)),
            test__engagement__product=new_finding.test.engagement.product).exclude(
                id=new_finding.id).exclude(
                    hash_code=None).exclude(
                        duplicate=True)
    deduplicationLogger.debug("Found " +
        str(len(existing_findings)) + " findings with either the same unique_id_from_tool or hash_code")
    for find in existing_findings:
        if is_deduplication_on_engagement_mismatch(new_finding, find):
            deduplicationLogger.debug(
                'deduplication_on_engagement_mismatch, skipping dedupe.')
            continue
        try:
            set_duplicate(new_finding, find)
        except Exception as e:
            deduplicationLogger.debug(str(e))
            continue
        break


def set_duplicate(new_finding, existing_finding):
    if existing_finding.duplicate:
        raise Exception("Existing finding is a duplicate")
    if existing_finding.id == new_finding.id:
        raise Exception("Can not add duplicate to itself")
    deduplicationLogger.debug('New finding ' + str(new_finding.id) + ' is a duplicate of existing finding ' + str(existing_finding.id))
    if (existing_finding.is_Mitigated or existing_finding.mitigated) and new_finding.active and not new_finding.is_Mitigated:
        existing_finding.mitigated = new_finding.mitigated
        existing_finding.is_Mitigated = new_finding.is_Mitigated
        existing_finding.active = new_finding.active
        existing_finding.verified = new_finding.verified
        existing_finding.notes.create(author=existing_finding.reporter,
                                      entry="This finding has been automatically re-openend as it was found in recent scans.")
        existing_finding.save()
    new_finding.duplicate = True
    new_finding.active = False
    new_finding.verified = False
    new_finding.duplicate_finding = existing_finding
    for find in new_finding.original_finding.all():
        new_finding.original_finding.remove(find)
        set_duplicate(find, existing_finding)
    existing_finding.found_by.add(new_finding.test.test_type)
    super(Finding, new_finding).save()
    super(Finding, existing_finding).save()


def removeLoop(finding_id, counter):
    # get latest status
    finding = Finding.objects.get(id=finding_id)
    real_original = finding.duplicate_finding

    if not real_original or real_original is None:
        return

    if finding_id == real_original.id:
        finding.duplicate_finding = None
        super(Finding, finding).save()
        return

    # Only modify the findings if the original ID is lower to get the oldest finding as original
    if (real_original.id > finding_id) and (real_original.duplicate_finding is not None):
        tmp = finding_id
        finding_id = real_original.id
        real_original = Finding.objects.get(id=tmp)
        finding = Finding.objects.get(id=finding_id)

    if real_original in finding.original_finding.all():
        # remove the original from the duplicate list if it is there
        finding.original_finding.remove(real_original)
        super(Finding, finding).save()
    if counter <= 0:
        # Maximum recursion depth as safety method to circumvent recursion here
        return
    for f in finding.original_finding.all():
        # for all duplicates set the original as their original, get rid of self in between
        f.duplicate_finding = real_original
        super(Finding, f).save()
        super(Finding, real_original).save()
        removeLoop(f.id, counter - 1)


def fix_loop_duplicates():
    candidates = Finding.objects.filter(duplicate_finding__isnull=False, original_finding__isnull=False).all().order_by("-id")
    deduplicationLogger.info("Identified %d Findings with Loops" % len(candidates))
    for find_id in candidates.values_list('id', flat=True):
        removeLoop(find_id, 5)

    new_originals = Finding.objects.filter(duplicate_finding__isnull=True, duplicate=True)
    for f in new_originals:
        deduplicationLogger.info("New Original: %d " % f.id)
        f.duplicate = False
        super(Finding, f).save()

    loop_count = Finding.objects.filter(duplicate_finding__isnull=False, original_finding__isnull=False).count()
    deduplicationLogger.info("%d Finding found with Loops" % loop_count)


def rename_whitesource_finding():
    whitesource_id = Test_Type.objects.get(name="Whitesource Scan").id
    findings = Finding.objects.filter(found_by=whitesource_id)
    findings = findings.order_by('-pk')
    logger.info("######## Updating Hashcodes - deduplication is done in background using django signals upon finding save ########")
    for finding in findings:
        logger.info("Updating Whitesource Finding with id: %d" % finding.id)
        lib_name_begin = re.search('\\*\\*Library Filename\\*\\* : ', finding.description).span(0)[1]
        lib_name_end = re.search('\\*\\*Library Description\\*\\*', finding.description).span(0)[0]
        lib_name = finding.description[lib_name_begin:lib_name_end - 1]
        if finding.cve is None:
            finding.title = "CVE-None | " + lib_name
        else:
            finding.title = finding.cve + " | " + lib_name
        if not finding.cwe:
            logger.debug('Set cwe for finding %d to 1035 if not an cwe Number is set' % finding.id)
            finding.cwe = 1035
        finding.title = finding.title.rstrip()  # delete \n at the end of the title
        from titlecase import titlecase
        finding.title = titlecase(finding.title)
        finding.hash_code = finding.compute_hash_code()
        finding.save()


def sync_rules(new_finding, *args, **kwargs):
    rules = Rule.objects.filter(applies_to='Finding', parent_rule=None)
    for rule in rules:
        child_val = True
        child_list = [val for val in rule.child_rules.all()]
        while (len(child_list) != 0):
            child_val = child_val and child_rule(child_list.pop(), new_finding)
        if child_val:
            if rule.operator == 'Matches':
                if getattr(new_finding, rule.match_field) == rule.match_text:
                    if rule.application == 'Append':
                        set_attribute_rule(new_finding, rule, (getattr(
                            new_finding, rule.applied_field) + rule.text))
                    else:
                        set_attribute_rule(new_finding, rule, rule.text)
                        new_finding.save(dedupe_option=False,
                                         rules_option=False)
            else:
                if rule.match_text in getattr(new_finding, rule.match_field):
                    if rule.application == 'Append':
                        set_attribute_rule(new_finding, rule, (getattr(
                            new_finding, rule.applied_field) + rule.text))
                    else:
                        set_attribute_rule(new_finding, rule, rule.text)
                        new_finding.save(dedupe_option=False,
                                         rules_option=False)


def set_attribute_rule(new_finding, rule, value):
    if rule.text == "True":
        setattr(new_finding, rule.applied_field, True)
    elif rule.text == "False":
        setattr(new_finding, rule.applied_field, False)
    else:
        setattr(new_finding, rule.applied_field, value)


def child_rule(rule, new_finding):
    if rule.operator == 'Matches':
        if getattr(new_finding, rule.match_field) == rule.match_text:
            return True
        else:
            return False
    else:
        if rule.match_text in getattr(new_finding, rule.match_field):
            return True
        else:
            return False


def count_findings(findings):
    product_count = {}
    finding_count = {'low': 0, 'med': 0, 'high': 0, 'crit': 0}
    for f in findings:
        product = f.test.engagement.product
        if product in product_count:
            product_count[product][4] += 1
            if f.severity == 'Low':
                product_count[product][3] += 1
                finding_count['low'] += 1
            if f.severity == 'Medium':
                product_count[product][2] += 1
                finding_count['med'] += 1
            if f.severity == 'High':
                product_count[product][1] += 1
                finding_count['high'] += 1
            if f.severity == 'Critical':
                product_count[product][0] += 1
                finding_count['crit'] += 1
        else:
            product_count[product] = [0, 0, 0, 0, 0]
            product_count[product][4] += 1
            if f.severity == 'Low':
                product_count[product][3] += 1
                finding_count['low'] += 1
            if f.severity == 'Medium':
                product_count[product][2] += 1
                finding_count['med'] += 1
            if f.severity == 'High':
                product_count[product][1] += 1
                finding_count['high'] += 1
            if f.severity == 'Critical':
                product_count[product][0] += 1
                finding_count['crit'] += 1
    return product_count, finding_count


def findings_this_period(findings, period_type, stuff, o_stuff, a_stuff):
    # periodType: 0 - weeks
    # 1 - months
    now = timezone.now()
    for i in range(6):
        counts = []
        # Weeks start on Monday
        if period_type == 0:
            curr = now - relativedelta(weeks=i)
            start_of_period = curr - relativedelta(
                weeks=1, weekday=0, hour=0, minute=0, second=0)
            end_of_period = curr + relativedelta(
                weeks=0, weekday=0, hour=0, minute=0, second=0)
        else:
            curr = now - relativedelta(months=i)
            start_of_period = curr - relativedelta(
                day=1, hour=0, minute=0, second=0)
            end_of_period = curr + relativedelta(
                day=31, hour=23, minute=59, second=59)

        o_count = {
            'closed': 0,
            'zero': 0,
            'one': 0,
            'two': 0,
            'three': 0,
            'total': 0
        }
        a_count = {
            'closed': 0,
            'zero': 0,
            'one': 0,
            'two': 0,
            'three': 0,
            'total': 0
        }
        for f in findings:
            if f.mitigated is not None and end_of_period >= f.mitigated >= start_of_period:
                o_count['closed'] += 1
            elif f.mitigated is not None and f.mitigated > end_of_period and f.date <= end_of_period.date(
            ):
                if f.severity == 'Critical':
                    o_count['zero'] += 1
                elif f.severity == 'High':
                    o_count['one'] += 1
                elif f.severity == 'Medium':
                    o_count['two'] += 1
                elif f.severity == 'Low':
                    o_count['three'] += 1
            elif f.mitigated is None and f.date <= end_of_period.date():
                if f.severity == 'Critical':
                    o_count['zero'] += 1
                elif f.severity == 'High':
                    o_count['one'] += 1
                elif f.severity == 'Medium':
                    o_count['two'] += 1
                elif f.severity == 'Low':
                    o_count['three'] += 1
            elif f.mitigated is None and f.date <= end_of_period.date():
                if f.severity == 'Critical':
                    a_count['zero'] += 1
                elif f.severity == 'High':
                    a_count['one'] += 1
                elif f.severity == 'Medium':
                    a_count['two'] += 1
                elif f.severity == 'Low':
                    a_count['three'] += 1

        total = sum(o_count.values()) - o_count['closed']
        if period_type == 0:
            counts.append(
                start_of_period.strftime("%b %d") + " - " +
                end_of_period.strftime("%b %d"))
        else:
            counts.append(start_of_period.strftime("%b %Y"))
        counts.append(o_count['zero'])
        counts.append(o_count['one'])
        counts.append(o_count['two'])
        counts.append(o_count['three'])
        counts.append(total)
        counts.append(o_count['closed'])

        stuff.append(counts)
        o_stuff.append(counts[:-1])

        a_counts = []
        a_total = sum(a_count.values())
        if period_type == 0:
            a_counts.append(
                start_of_period.strftime("%b %d") + " - " +
                end_of_period.strftime("%b %d"))
        else:
            a_counts.append(start_of_period.strftime("%b %Y"))
        a_counts.append(a_count['zero'])
        a_counts.append(a_count['one'])
        a_counts.append(a_count['two'])
        a_counts.append(a_count['three'])
        a_counts.append(a_total)
        a_stuff.append(a_counts)


def add_breadcrumb(parent=None,
                   title=None,
                   top_level=True,
                   url=None,
                   request=None,
                   clear=False):
    title_done = False
    if clear:
        request.session['dojo_breadcrumbs'] = None
        return
    else:
        crumbs = request.session.get('dojo_breadcrumbs', None)

    if top_level or crumbs is None:
        crumbs = [
            {
                'title': 'Home',
                'url': reverse('home')
            },
        ]
        if parent is not None and getattr(parent, "get_breadcrumbs", None):
            crumbs += parent.get_breadcrumbs()
        else:
            title_done = True
            crumbs += [{
                'title': title,
                'url': request.get_full_path() if url is None else url
            }]
    else:
        resolver = get_resolver(None).resolve
        if parent is not None and getattr(parent, "get_breadcrumbs", None):
            obj_crumbs = parent.get_breadcrumbs()
            if title is not None:
                obj_crumbs += [{
                    'title':
                    title,
                    'url':
                    request.get_full_path() if url is None else url
                }]
        else:
            title_done = True
            obj_crumbs = [{
                'title':
                title,
                'url':
                request.get_full_path() if url is None else url
            }]

        for crumb in crumbs:
            crumb_to_resolve = crumb['url'] if '?' not in crumb[
                'url'] else crumb['url'][:crumb['url'].index('?')]
            crumb_view = resolver(crumb_to_resolve)
            for obj_crumb in obj_crumbs:
                obj_crumb_to_resolve = obj_crumb[
                    'url'] if '?' not in obj_crumb['url'] else obj_crumb[
                        'url'][:obj_crumb['url'].index('?')]
                obj_crumb_view = resolver(obj_crumb_to_resolve)

                if crumb_view.view_name == obj_crumb_view.view_name:
                    if crumb_view.kwargs == obj_crumb_view.kwargs:
                        if len(obj_crumbs) == 1 and crumb in crumbs:
                            crumbs = crumbs[:crumbs.index(crumb)]
                        else:
                            obj_crumbs.remove(obj_crumb)
                    else:
                        if crumb in crumbs:
                            crumbs = crumbs[:crumbs.index(crumb)]

        crumbs += obj_crumbs

    request.session['dojo_breadcrumbs'] = crumbs


def get_punchcard_data(findings, start_date, weeks):
    # use try catch to make sure any teething bugs in the bunchcard don't break the dashboard
    try:
        # gather findings over past half year, make sure to start on a sunday
        first_sunday = start_date - relativedelta(weekday=SU(-1))
        last_sunday = start_date + relativedelta(weeks=weeks)

        # reminder: The first week of a year is the one that contains the year???s first Thursday
        # so we could have for 29/12/2019: week=1 and year=2019 :-D. So using week number from db is not practical

        severities_by_day = findings.filter(created__date__gte=first_sunday).filter(created__date__lt=last_sunday) \
                                    .values('created__date') \
                                    .annotate(count=Count('id')) \
                                    .order_by('created__date')

        # return empty stuff if no findings to be statted
        if severities_by_day.count() <= 0:
            return None, None

        # day of the week numbers:
        # javascript  database python
        # sun 6         1       6
        # mon 5         2       0
        # tue 4         3       1
        # wed 3         4       2
        # thu 2         5       3
        # fri 1         6       4
        # sat 0         7       5

        # map from python to javascript, do not use week numbers or day numbers from database.
        day_offset = {0: 5, 1: 4, 2: 3, 3: 2, 4: 1, 5: 0, 6: 6}

        punchcard = list()
        ticks = list()
        highest_day_count = 0
        tick = 0
        day_counts = [0, 0, 0, 0, 0, 0, 0]

        start_of_week = timezone.make_aware(datetime.combine(first_sunday, datetime.min.time()))
        start_of_next_week = start_of_week + relativedelta(weeks=1)
        day_counts = [0, 0, 0, 0, 0, 0, 0]

        for day in severities_by_day:
            created = day['created__date']
            day_count = day['count']

            created = timezone.make_aware(datetime.combine(created, datetime.min.time()))

            # print('%s %s %s', created, created.weekday(), calendar.day_name[created.weekday()], day_count)

            if created < start_of_week:
                raise ValueError('date found outside supported range: ' + str(created))
            else:
                if created >= start_of_week and created < start_of_next_week:
                    # add day count to current week data
                    day_counts[day_offset[created.weekday()]] = day_count
                    highest_day_count = max(highest_day_count, day_count)
                else:
                    # created >= start_of_next_week, so store current week, prepare for next
                    while created >= start_of_next_week:
                        week_data, label = get_week_data(start_of_week, tick, day_counts)
                        punchcard.extend(week_data)
                        ticks.append(label)
                        tick += 1

                        # new week, new values!
                        day_counts = [0, 0, 0, 0, 0, 0, 0]
                        start_of_week = start_of_next_week
                        start_of_next_week += relativedelta(weeks=1)

                    # finally a day that falls into the week bracket
                    day_counts[day_offset[created.weekday()]] = day_count
                    highest_day_count = max(highest_day_count, day_count)

        # add week in progress + empty weeks on the end if needed
        while tick < weeks + 1:
            # print(tick)
            week_data, label = get_week_data(start_of_week, tick, day_counts)
            # print(week_data, label)
            punchcard.extend(week_data)
            ticks.append(label)
            tick += 1

            day_counts = [0, 0, 0, 0, 0, 0, 0]
            start_of_week = start_of_next_week
            start_of_next_week += relativedelta(weeks=1)

        # adjust the size or circles
        ratio = (sqrt(highest_day_count / pi))
        for punch in punchcard:
            # front-end needs both the count for the label and the ratios of the radii of the circles
            punch.append(punch[2])
            punch[2] = (sqrt(punch[2] / pi)) / ratio

        return punchcard, ticks

    except Exception as e:
        logger.exception('Not showing punchcard graph due to exception gathering data', e)
        return None, None


def get_week_data(week_start_date, tick, day_counts):
    data = []
    for i in range(0, len(day_counts)):
        data.append([tick, i, day_counts[i]])
    label = [tick, week_start_date.strftime("<span class='small'>%m/%d<br/>%Y</span>")]
    return data, label


# 5 params
def get_period_counts_legacy(findings,
                             findings_closed,
                             accepted_findings,
                             period_interval,
                             start_date,
                             relative_delta='months'):
    opened_in_period = list()
    accepted_in_period = list()
    opened_in_period.append(
        ['Timestamp', 'Date', 'S0', 'S1', 'S2', 'S3', 'Total', 'Closed'])
    accepted_in_period.append(
        ['Timestamp', 'Date', 'S0', 'S1', 'S2', 'S3', 'Total', 'Closed'])

    for x in range(-1, period_interval):
        if relative_delta == 'months':
            # make interval the first through last of month
            end_date = (start_date + relativedelta(months=x)) + relativedelta(
                day=1, months=+1, days=-1)
            new_date = (
                start_date + relativedelta(months=x)) + relativedelta(day=1)
        else:
            # week starts the monday before
            new_date = start_date + relativedelta(weeks=x, weekday=MO(1))
            end_date = new_date + relativedelta(weeks=1, weekday=MO(1))

        closed_in_range_count = findings_closed.filter(
            mitigated__date__range=[new_date, end_date]).count()

        if accepted_findings:
            risks_a = accepted_findings.filter(
                risk_acceptance__created__date__range=[
                    datetime(
                        new_date.year,
                        new_date.month,
                        1,
                        tzinfo=timezone.get_current_timezone()),
                    datetime(
                        new_date.year,
                        new_date.month,
                        monthrange(new_date.year, new_date.month)[1],
                        tzinfo=timezone.get_current_timezone())
                ])
        else:
            risks_a = None

        crit_count, high_count, med_count, low_count, closed_count = [
            0, 0, 0, 0, 0
        ]
        for finding in findings:
            if new_date <= datetime.combine(finding.date, datetime.min.time(
            )).replace(tzinfo=timezone.get_current_timezone()) <= end_date:
                if finding.severity == 'Critical':
                    crit_count += 1
                elif finding.severity == 'High':
                    high_count += 1
                elif finding.severity == 'Medium':
                    med_count += 1
                elif finding.severity == 'Low':
                    low_count += 1

        total = crit_count + high_count + med_count + low_count
        opened_in_period.append(
            [(tcalendar.timegm(new_date.timetuple()) * 1000), new_date,
             crit_count, high_count, med_count, low_count, total,
             closed_in_range_count])
        crit_count, high_count, med_count, low_count, closed_count = [
            0, 0, 0, 0, 0
        ]
        if risks_a is not None:
            for finding in risks_a:
                if finding.severity == 'Critical':
                    crit_count += 1
                elif finding.severity == 'High':
                    high_count += 1
                elif finding.severity == 'Medium':
                    med_count += 1
                elif finding.severity == 'Low':
                    low_count += 1

        total = crit_count + high_count + med_count + low_count
        accepted_in_period.append(
            [(tcalendar.timegm(new_date.timetuple()) * 1000), new_date,
             crit_count, high_count, med_count, low_count, total])

    return {
        'opened_per_period': opened_in_period,
        'accepted_per_period': accepted_in_period
    }


def get_period_counts(active_findings,
                      findings,
                      findings_closed,
                      accepted_findings,
                      period_interval,
                      start_date,
                      relative_delta='months'):
    start_date = datetime(
        start_date.year,
        start_date.month,
        start_date.day,
        tzinfo=timezone.get_current_timezone())
    opened_in_period = list()
    active_in_period = list()
    accepted_in_period = list()
    opened_in_period.append(
        ['Timestamp', 'Date', 'S0', 'S1', 'S2', 'S3', 'Total', 'Closed'])
    active_in_period.append(
        ['Timestamp', 'Date', 'S0', 'S1', 'S2', 'S3', 'Total', 'Closed'])
    accepted_in_period.append(
        ['Timestamp', 'Date', 'S0', 'S1', 'S2', 'S3', 'Total', 'Closed'])

    for x in range(-1, period_interval):
        if relative_delta == 'months':
            # make interval the first through last of month
            end_date = (start_date + relativedelta(months=x)) + relativedelta(
                day=1, months=+1, days=-1)
            new_date = (
                start_date + relativedelta(months=x)) + relativedelta(day=1)
        else:
            # week starts the monday before
            new_date = start_date + relativedelta(weeks=x, weekday=MO(1))
            end_date = new_date + relativedelta(weeks=1, weekday=MO(1))

        closed_in_range_count = findings_closed.filter(
            mitigated__date__range=[new_date, end_date]).count()

        if accepted_findings:
            risks_a = accepted_findings.filter(
                risk_acceptance__created__date__range=[
                    datetime(
                        new_date.year,
                        new_date.month,
                        1,
                        tzinfo=timezone.get_current_timezone()),
                    datetime(
                        new_date.year,
                        new_date.month,
                        monthrange(new_date.year, new_date.month)[1],
                        tzinfo=timezone.get_current_timezone())
                ])
        else:
            risks_a = None

        crit_count, high_count, med_count, low_count, closed_count = [
            0, 0, 0, 0, 0
        ]
        for finding in findings:
            try:
                if new_date <= datetime.combine(
                        finding.date, datetime.min.time()
                ).replace(tzinfo=timezone.get_current_timezone()) <= end_date:
                    if finding.severity == 'Critical':
                        crit_count += 1
                    elif finding.severity == 'High':
                        high_count += 1
                    elif finding.severity == 'Medium':
                        med_count += 1
                    elif finding.severity == 'Low':
                        low_count += 1
            except:
                if new_date <= finding.date <= end_date:
                    if finding.severity == 'Critical':
                        crit_count += 1
                    elif finding.severity == 'High':
                        high_count += 1
                    elif finding.severity == 'Medium':
                        med_count += 1
                    elif finding.severity == 'Low':
                        low_count += 1
                pass

        total = crit_count + high_count + med_count + low_count
        opened_in_period.append(
            [(tcalendar.timegm(new_date.timetuple()) * 1000), new_date,
             crit_count, high_count, med_count, low_count, total,
             closed_in_range_count])
        crit_count, high_count, med_count, low_count, closed_count = [
            0, 0, 0, 0, 0
        ]
        if risks_a is not None:
            for finding in risks_a:
                if finding.severity == 'Critical':
                    crit_count += 1
                elif finding.severity == 'High':
                    high_count += 1
                elif finding.severity == 'Medium':
                    med_count += 1
                elif finding.severity == 'Low':
                    low_count += 1

        total = crit_count + high_count + med_count + low_count
        accepted_in_period.append(
            [(tcalendar.timegm(new_date.timetuple()) * 1000), new_date,
             crit_count, high_count, med_count, low_count, total])
        crit_count, high_count, med_count, low_count, closed_count = [
            0, 0, 0, 0, 0
        ]
        for finding in active_findings:
            try:
                if datetime.combine(finding.date, datetime.min.time()).replace(
                        tzinfo=timezone.get_current_timezone()) <= end_date:
                    if finding.severity == 'Critical':
                        crit_count += 1
                    elif finding.severity == 'High':
                        high_count += 1
                    elif finding.severity == 'Medium':
                        med_count += 1
                    elif finding.severity == 'Low':
                        low_count += 1
            except:
                if finding.date <= end_date:
                    if finding.severity == 'Critical':
                        crit_count += 1
                    elif finding.severity == 'High':
                        high_count += 1
                    elif finding.severity == 'Medium':
                        med_count += 1
                    elif finding.severity == 'Low':
                        low_count += 1
                pass
        total = crit_count + high_count + med_count + low_count
        active_in_period.append(
            [(tcalendar.timegm(new_date.timetuple()) * 1000), new_date,
             crit_count, high_count, med_count, low_count, total])

    return {
        'opened_per_period': opened_in_period,
        'accepted_per_period': accepted_in_period,
        'active_per_period': active_in_period
    }


def opened_in_period(start_date, end_date, pt):
    start_date = datetime(
        start_date.year,
        start_date.month,
        start_date.day,
        tzinfo=timezone.get_current_timezone())
    end_date = datetime(
        end_date.year,
        end_date.month,
        end_date.day,
        tzinfo=timezone.get_current_timezone())
    opened_in_period = Finding.objects.filter(
        date__range=[start_date, end_date],
        test__engagement__product__prod_type=pt,
        verified=True,
        false_p=False,
        duplicate=False,
        out_of_scope=False,
        mitigated__isnull=True,
        severity__in=(
            'Critical', 'High', 'Medium',
            'Low')).values('numerical_severity').annotate(
                Count('numerical_severity')).order_by('numerical_severity')
    total_opened_in_period = Finding.objects.filter(
        date__range=[start_date, end_date],
        test__engagement__product__prod_type=pt,
        verified=True,
        false_p=False,
        duplicate=False,
        out_of_scope=False,
        mitigated__isnull=True,
        severity__in=('Critical', 'High', 'Medium', 'Low')).aggregate(
            total=Sum(
                Case(
                    When(
                        severity__in=('Critical', 'High', 'Medium', 'Low'),
                        then=Value(1)),
                    output_field=IntegerField())))['total']

    oip = {
        'S0':
        0,
        'S1':
        0,
        'S2':
        0,
        'S3':
        0,
        'Total':
        total_opened_in_period,
        'start_date':
        start_date,
        'end_date':
        end_date,
        'closed':
        Finding.objects.filter(
            mitigated__date__range=[start_date, end_date],
            test__engagement__product__prod_type=pt,
            severity__in=('Critical', 'High', 'Medium', 'Low')).aggregate(
                total=Sum(
                    Case(
                        When(
                            severity__in=('Critical', 'High', 'Medium', 'Low'),
                            then=Value(1)),
                        output_field=IntegerField())))['total'],
        'to_date_total':
        Finding.objects.filter(
            date__lte=end_date.date(),
            verified=True,
            false_p=False,
            duplicate=False,
            out_of_scope=False,
            mitigated__isnull=True,
            test__engagement__product__prod_type=pt,
            severity__in=('Critical', 'High', 'Medium', 'Low')).count()
    }

    for o in opened_in_period:
        oip[o['numerical_severity']] = o['numerical_severity__count']

    return oip


def message(count, noun, verb):
    return ('{} ' + noun + '{} {} ' + verb).format(
        count, pluralize(count), pluralize(count, 'was,were'))


class FileIterWrapper(object):
    def __init__(self, flo, chunk_size=1024**2):
        self.flo = flo
        self.chunk_size = chunk_size

    def __next__(self):
        data = self.flo.read(self.chunk_size)
        if data:
            return data
        else:
            raise StopIteration

    def __iter__(self):
        return self


def get_cal_event(start_date, end_date, summary, description, uid):
    cal = vobject.iCalendar()
    cal.add('vevent')
    cal.vevent.add('summary').value = summary
    cal.vevent.add('description').value = description
    start = cal.vevent.add('dtstart')
    start.value = start_date
    end = cal.vevent.add('dtend')
    end.value = end_date
    cal.vevent.add('uid').value = uid
    return cal


def named_month(month_number):
    """
    Return the name of the month, given the number.
    """
    return date(1900, month_number, 1).strftime("%B")


def normalize_query(query_string,
                    findterms=re.compile(r'"([^"]+)"|(\S+)').findall,
                    normspace=re.compile(r'\s{2,}').sub):
    return [
        normspace(' ', (t[0] or t[1]).strip()) for t in findterms(query_string)
    ]


def build_query(query_string, search_fields):
    """ Returns a query, that is a combination of Q objects. That combination
    aims to search keywords within a model by testing the given search fields.

    """
    query = None  # Query to search for every search term
    terms = normalize_query(query_string)
    for term in terms:
        or_query = None  # Query to search for a given term in each field
        for field_name in search_fields:
            q = Q(**{"%s__icontains" % field_name: term})

            if or_query:
                or_query = or_query | q
            else:
                or_query = q

        if query:
            query = query & or_query
        else:
            query = or_query
    return query


def template_search_helper(fields=None, query_string=None):
    if not fields:
        fields = [
            'title',
            'description',
        ]
    findings = Finding_Template.objects.all()

    if not query_string:
        return findings

    entry_query = build_query(query_string, fields)
    found_entries = findings.filter(entry_query)

    return found_entries


def get_page_items(request, items, page_size, param_name='page'):
    size = request.GET.get('page_size', page_size)
    paginator = Paginator(items, size)
    page = request.GET.get(param_name)

    # new get_page method will handle invalid page value, out of bounds pages, etc
    return paginator.get_page(page)


def get_page_items_and_count(request, items, page_size, param_name='page'):
    size = request.GET.get('page_size', page_size)
    paginator = Paginator(items, size)
    page = request.GET.get(param_name)

    # new get_page method will handle invalid page value, out of bounds pages, etc
    return paginator.get_page(page), paginator.count


def handle_uploaded_threat(f, eng):
    name, extension = os.path.splitext(f.name)
    with open(settings.MEDIA_ROOT + '/threat/%s%s' % (eng.id, extension),
              'wb+') as destination:
        for chunk in f.chunks():
            destination.write(chunk)
    eng.tmodel_path = settings.MEDIA_ROOT + '/threat/%s%s' % (eng.id,
                                                              extension)
    eng.save()


def handle_uploaded_selenium(f, cred):
    name, extension = os.path.splitext(f.name)
    with open(settings.MEDIA_ROOT + '/selenium/%s%s' % (cred.id, extension),
              'wb+') as destination:
        for chunk in f.chunks():
            destination.write(chunk)
    cred.selenium_script = settings.MEDIA_ROOT + '/selenium/%s%s' % (cred.id,
                                                                     extension)
    cred.save()


# Gets a connection to a Jira server based on the finding
def get_jira_connection(finding):
    jira = None
    prod = Product.objects.get(
        engagement=Engagement.objects.get(test=finding.test))

    try:
        jpkey = JIRA_PKey.objects.get(product=prod)
        jira_conf = jpkey.conf
        if jira_conf is not None:
            jira = JIRA(
                server=jira_conf.url,
                basic_auth=(jira_conf.username, jira_conf.password))
    except JIRA_PKey.DoesNotExist:
        pass
    return jira


def jira_get_resolution_id(jira, issue, status):
    transitions = jira.transitions(issue)
    resolution_id = None
    for t in transitions:
        if t['name'] == "Resolve Issue":
            resolution_id = t['id']
            break
        if t['name'] == "Reopen Issue":
            resolution_id = t['id']
            break

    return resolution_id


def jira_change_resolution_id(jira, issue, id):
    jira.transition_issue(issue, id)


# Logs the error to the alerts table, which appears in the notification toolbar
def log_jira_generic_alert(title, description):
    create_notification(
        event='jira_update',
        title=title,
        description=description,
        icon='bullseye',
        source='Jira')


# Logs the error to the alerts table, which appears in the notification toolbar
def log_jira_alert(error, finding):
    prod_name = finding.test.engagement.product.name
    create_notification(
        event='jira_update',
        title='Jira update issue (' + truncate_with_dots(prod_name, 25) + ')',
        description='Finding: ' + str(finding.id) + ', ' + error,
        url=reverse('view_finding', args=(finding.id, )),
        icon='bullseye',
        source='Jira')


# Displays an alert for Jira notifications
def log_jira_message(text, finding):
    create_notification(
        event='jira_update',
        title='Jira update message',
        description=text + " Finding: " + str(finding.id),
        url=reverse('view_finding', args=(finding.id, )),
        icon='bullseye',
        source='Jira')


def get_labels(find):
    # Update Label with system setttings label
    labels = []
    system_settings = System_Settings.objects.get()
    system_labels = system_settings.jira_labels
    if system_labels is None:
        return
    else:
        system_labels = system_labels.split()
    if len(system_labels) > 0:
        for system_label in system_labels:
            labels.append(system_label)
    # Update the label with the product name (underscore)
    prod_name = find.test.engagement.product.name.replace(" ", "_")
    labels.append(prod_name)
    return labels


def jira_description(find):
    template = 'issue-trackers/jira-description.tpl'
    kwargs = {}
    kwargs['finding'] = find
    kwargs['jira_conf'] = find.jira_conf_new()
    return render_to_string(template, kwargs)


def add_external_issue(find, external_issue_provider):
    eng = Engagement.objects.get(test=find.test)
    prod = Product.objects.get(engagement=eng)
    logger.debug('adding external issue with provider: ' + external_issue_provider)

    if external_issue_provider == 'github':
        add_external_issue_github(find, prod, eng)


def update_external_issue(find, old_status, external_issue_provider):
    prod = Product.objects.get(engagement=Engagement.objects.get(test=find.test))
    eng = Engagement.objects.get(test=find.test)

    if external_issue_provider == 'github':
        update_external_issue_github(find, prod, eng)


def close_external_issue(find, note, external_issue_provider):
    prod = Product.objects.get(engagement=Engagement.objects.get(test=find.test))
    eng = Engagement.objects.get(test=find.test)

    if external_issue_provider == 'github':
        close_external_issue_github(find, note, prod, eng)


def reopen_external_issue(find, note, external_issue_provider):
    prod = Product.objects.get(engagement=Engagement.objects.get(test=find.test))
    eng = Engagement.objects.get(test=find.test)

    if external_issue_provider == 'github':
        reopen_external_issue_github(find, note, prod, eng)


def add_issue(find, push_to_jira):
    eng = Engagement.objects.get(test=find.test)
    prod = Product.objects.get(engagement=eng)
    jira_minimum_threshold = Finding.get_number_severity(System_Settings.objects.get().jira_minimum_severity)

    if push_to_jira:
        if JIRA_PKey.objects.filter(product=prod).count() == 0:
            logger.error("Finding {} cannot be pushed to JIRA as there is no JIRA configuration for this product.".format(find.id))
            log_jira_alert('Finding cannot be pushed to JIRA as there is no JIRA configuration for this product.', find)
            return

        jpkey = JIRA_PKey.objects.get(product=prod)
        jira_conf = jpkey.conf

        if 'Active' in find.status() and 'Verified' in find.status():
            if jira_minimum_threshold > Finding.get_number_severity(find.severity):
                log_jira_alert('Finding below the minimum jira severity threshold.', find)
                logger.warn("Finding {} is below the minimum jira severity threshold.".format(find.id))
                logger.warn("The JIRA issue will NOT be created.")
                return

            logger.debug('Trying to create a new JIRA issue for finding {}...'.format(find.id))
            try:
                JIRAError.log_to_tempfile = False
                jira = JIRA(
                    server=jira_conf.url,
                    basic_auth=(jira_conf.username, jira_conf.password))

                meta = None

                fields = {
                        'project': {
                            'key': jpkey.project_key
                        },
                        'summary': find.title,
                        'description': jira_description(find),
                        'issuetype': {
                            'name': jira_conf.default_issue_type
                        },
                        'priority': {
                            'name': jira_conf.get_priority(find.severity)
                        }
                }

                if jpkey.component:
                    fields['components'] = [
                            {
                                'name': jpkey.component
                            },
                    ]

                labels = get_labels(find)
                if labels:
                    fields['labels'] = labels

                if System_Settings.objects.get().enable_finding_sla:
                    # populate duedate field, but only if it's available for this project + issuetype
                    # meta = jira.createmeta(projectKeys=jpkey.project_key, expand="fields")
                    if not meta:
                        meta = jira_meta(jira, jpkey)

                    if 'duedate' in meta['projects'][0]['issuetypes'][0]['fields']:
                        # jira wants YYYY-MM-DD
                        duedate = find.sla_deadline().strftime('%Y-%m-%d')
                        fields['duedate'] = duedate

                if len(find.endpoints.all()) > 0:
                    if not meta:
                        meta = jira_meta(jira, jpkey)

                    if 'environment' in meta['projects'][0]['issuetypes'][0]['fields']:
                        environment = "\n".join([str(endpoint) for endpoint in find.endpoints.all()])
                        fields['environment'] = environment

                logger.debug('sending fields to JIRA: %s', fields)

                new_issue = jira.create_issue(fields)

                j_issue = JIRA_Issue(
                    jira_id=new_issue.id, jira_key=new_issue, finding=find)
                j_issue.save()
                issue = jira.issue(new_issue.id)

                # Upload dojo finding screenshots to Jira
                for pic in find.images.all():
                    jira_attachment(
                        jira, issue,
                        settings.MEDIA_ROOT + pic.image_large.name)

                    # if jpkey.enable_engagement_epic_mapping:
                    #      epic = JIRA_Issue.objects.get(engagement=eng)
                    #      issue_list = [j_issue.jira_id,]
                    #      jira.add_issues_to_epic(epic_id=epic.jira_id, issue_keys=[str(j_issue.jira_id)], ignore_epics=True)
            except JIRAError as e:
                logger.error(e.text, find)
                log_jira_alert(e.text, find)
        else:
            log_jira_alert("A Finding needs to be both Active and Verified to be pushed to JIRA.", find)
            logger.warning("A Finding needs to be both Active and Verified to be pushed to JIRA.", find)


def jira_meta(jira, jpkey):
    return jira.createmeta(projectKeys=jpkey.project_key, issuetypeNames=jpkey.conf.default_issue_type, expand="projects.issuetypes.fields")


def jira_attachment(jira, issue, file, jira_filename=None):
    basename = file
    if jira_filename is None:
        basename = os.path.basename(file)

    # Check to see if the file has been uploaded to Jira
    if jira_check_attachment(issue, basename) is False:
        try:
            if jira_filename is not None:
                attachment = io.StringIO()
                attachment.write(jira_filename)
                jira.add_attachment(
                    issue=issue, attachment=attachment, filename=jira_filename)
            else:
                # read and upload a file
                with open(file, 'rb') as f:
                    jira.add_attachment(issue=issue, attachment=f)
        except JIRAError as e:
            log_jira_alert("Attachment: " + e.text)


def jira_check_attachment(issue, source_file_name):
    file_exists = False
    for attachment in issue.fields.attachment:
        filename = attachment.filename

        if filename == source_file_name:
            file_exists = True
            break

    return file_exists


def update_issue(find, push_to_jira):
    prod = Product.objects.get(
        engagement=Engagement.objects.get(test=find.test))
    jpkey = JIRA_PKey.objects.get(product=prod)
    jira_conf = jpkey.conf

    if push_to_jira:
        j_issue = JIRA_Issue.objects.get(finding=find)
        try:
            JIRAError.log_to_tempfile = False
            jira = JIRA(
                server=jira_conf.url,
                basic_auth=(jira_conf.username, jira_conf.password))
            issue = jira.issue(j_issue.jira_id)

            meta = None

            fields = {}
            # Only update the component if it didn't exist earlier in Jira, this is to avoid assigning multiple components to an item
            if issue.fields.components:
                log_jira_alert(
                    "Component not updated, exists in Jira already. Update from Jira instead.",
                    find)
            elif jpkey.component:
                # Add component to the Jira issue
                component = [
                    {
                        'name': jpkey.component
                    },
                ]
                fields = {"components": component}

            labels = get_labels(find)
            if labels:
                fields['labels'] = labels

            if len(find.endpoints.all()) > 0:
                if not meta:
                    meta = jira_meta(jira, jpkey)

                if 'environment' in meta['projects'][0]['issuetypes'][0]['fields']:
                    environment = "\n".join([str(endpoint) for endpoint in find.endpoints.all()])
                    fields['environment'] = environment

            # Upload dojo finding screenshots to Jira
            for pic in find.images.all():
                jira_attachment(jira, issue,
                                settings.MEDIA_ROOT + pic.image_large.name)

            logger.debug('sending fields to JIRA: %s', fields)

            issue.update(
                summary=find.title,
                description=jira_description(find),
                priority={'name': jira_conf.get_priority(find.severity)},
                fields=fields)
            # print('\n\nSaving jira_change\n\n')
            # Moving this to finding.save()
            # find.jira_change = timezone.now()
            # find.save()
            # Add labels(security & product)

        except JIRAError as e:
            log_jira_alert(e.text, find)

        req_url = jira_conf.url + '/rest/api/latest/issue/' + \
            j_issue.jira_id + '/transitions'
        if 'Inactive' in find.status() or 'Mitigated' in find.status(
        ) or 'False Positive' in find.status(
        ) or 'Out of Scope' in find.status() or 'Duplicate' in find.status():
            # if 'Active' in old_status:
            json_data = {'transition': {'id': jira_conf.close_status_key}}
            r = requests.post(
                url=req_url,
                auth=HTTPBasicAuth(jira_conf.username, jira_conf.password),
                json=json_data)
            if r.status_code != 204:
                logger.warn("JIRA transition failed with error: {}".format(r.text))
            find.jira_change = timezone.now()
            find.save()
        elif 'Active' in find.status() and 'Verified' in find.status():
            # if 'Inactive' in old_status:
            json_data = {'transition': {'id': jira_conf.open_status_key}}
            r = requests.post(
                url=req_url,
                auth=HTTPBasicAuth(jira_conf.username, jira_conf.password),
                json=json_data)
            if r.status_code != 204:
                logger.warn("JIRA transition failed with error: {}".format(r.text))
            find.jira_change = timezone.now()
            find.save()


def close_epic(eng, push_to_jira):
    engagement = eng
    prod = Product.objects.get(engagement=engagement)
    jpkey = JIRA_PKey.objects.get(product=prod)
    jira_conf = jpkey.conf
    if jpkey.enable_engagement_epic_mapping and push_to_jira:
        try:
            j_issue = JIRA_Issue.objects.get(engagement=eng)
            req_url = jira_conf.url + '/rest/api/latest/issue/' + \
                j_issue.jira_id + '/transitions'
            j_issue = JIRA_Issue.objects.get(engagement=eng)
            json_data = {'transition': {'id': jira_conf.close_status_key}}
            r = requests.post(
                url=req_url,
                auth=HTTPBasicAuth(jira_conf.username, jira_conf.password),
                json=json_data)
            if r.status_code != 204:
                logger.warn("JIRA close epic failed with error: {}".format(r.text))
        except Exception as e:
            log_jira_generic_alert('Jira Engagement/Epic Close Error', str(e))
            pass


def update_epic(eng, push_to_jira):
    engagement = eng
    prod = Product.objects.get(engagement=engagement)
    jpkey = JIRA_PKey.objects.get(product=prod)
    jira_conf = jpkey.conf
    if jpkey.enable_engagement_epic_mapping and push_to_jira:
        try:
            jira = JIRA(
                server=jira_conf.url,
                basic_auth=(jira_conf.username, jira_conf.password))
            j_issue = JIRA_Issue.objects.get(engagement=eng)
            issue = jira.issue(j_issue.jira_id)
            issue.update(summary=eng.name, description=eng.name)
        except Exception as e:
            log_jira_generic_alert('Jira Engagement/Epic Update Error', str(e))
            pass


def add_epic(eng, push_to_jira):
    engagement = eng
    prod = Product.objects.get(engagement=engagement)
    jpkey = JIRA_PKey.objects.get(product=prod)
    jira_conf = jpkey.conf
    if jpkey.enable_engagement_epic_mapping and push_to_jira:
        issue_dict = {
            'project': {
                'key': jpkey.project_key
            },
            'summary': engagement.name,
            'description': engagement.name,
            'issuetype': {
                'name': 'Epic'
            },
            'customfield_' + str(jira_conf.epic_name_id): engagement.name,
        }
        try:
            jira = JIRA(
                server=jira_conf.url,
                basic_auth=(jira_conf.username, jira_conf.password))
            new_issue = jira.create_issue(fields=issue_dict)
            j_issue = JIRA_Issue(
                jira_id=new_issue.id,
                jira_key=new_issue,
                engagement=engagement)
            j_issue.save()
        except Exception as e:
            error = str(e)
            message = ""
            if "customfield" in error:
                message = "The 'Epic name id' in your DefectDojo Jira Configuration does not appear to be correct. Please visit, " + jira_conf.url + \
                    "/rest/api/2/field and search for Epic Name. Copy the number out of cf[number] and place in your DefectDojo settings for Jira and try again. For example, if your results are cf[100001] then copy 100001 and place it in 'Epic name id'. (Your Epic Id will be different.) \n\n"

            log_jira_generic_alert('Jira Engagement/Epic Creation Error',
                                   message + error)
            pass


def add_comment(find, note, force_push=False):
    if not note.private:
        prod = Product.objects.get(
            engagement=Engagement.objects.get(test=find.test))

        try:
            jpkey = JIRA_PKey.objects.get(product=prod)
            jira_conf = jpkey.conf

            if jpkey.push_notes or force_push is True:
                try:
                    jira = JIRA(
                        server=jira_conf.url,
                        basic_auth=(jira_conf.username, jira_conf.password))
                    j_issue = JIRA_Issue.objects.get(finding=find)
                    jira.add_comment(
                        j_issue.jira_id,
                        '(%s): %s' % (note.author.get_full_name(), note.entry))
                except Exception as e:
                    log_jira_generic_alert('Jira Add Comment Error', str(e))
                    pass
        except JIRA_PKey.DoesNotExist:
            pass


def send_review_email(request, user, finding, users, new_note):
    # TODO remove apparent dead code

    recipients = [u.email for u in users]
    msg = "\nGreetings, \n\n"
    msg += "{0} has requested that you please review ".format(str(user))
    msg += "the following finding for accuracy:"
    msg += "\n\n" + finding.title
    msg += "\n\nIt can be reviewed at " + request.build_absolute_uri(
        reverse("view_finding", args=(finding.id, )))
    msg += "\n\n{0} provided the following details:".format(str(user))
    msg += "\n\n" + new_note.entry
    msg += "\n\nThanks\n"

    send_mail(
        'DefectDojo Finding Review Request',
        msg,
        user.email,
        recipients,
        fail_silently=False)
    pass


def process_notifications(request, note, parent_url, parent_title):
    regex = re.compile(r'(?:\A|\s)@(\w+)\b')

    usernames_to_check = set([un.lower() for un in regex.findall(note.entry)])

    users_to_notify = [
        User.objects.filter(username=username).get()
        for username in usernames_to_check
        if User.objects.filter(is_active=True, username=username).exists()
    ]  # is_staff also?

    if len(note.entry) > 200:
        note.entry = note.entry[:200]
        note.entry += "..."

    create_notification(
        event='user_mentioned',
        section=parent_title,
        note=note,
        initiator=request.user,
        title='%s jotted a note' % request.user,
        url=parent_url,
        icon='commenting',
        recipients=users_to_notify)


def send_atmention_email(user, users, parent_url, parent_title, new_note):
    recipients = [u.email for u in users]
    msg = "\nGreetings, \n\n"
    msg += "User {0} mentioned you in a note on {1}".format(
        str(user), parent_title)
    msg += "\n\n" + new_note.entry
    msg += "\n\nIt can be reviewed at " + parent_url
    msg += "\n\nThanks\n"
    send_mail(
        'DefectDojo - {0} @mentioned you in a note'.format(str(user)),
        msg,
        user.email,
        recipients,
        fail_silently=False)


def encrypt(key, iv, plaintext):
    text = ""
    if plaintext and plaintext is not None:
        backend = default_backend()
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=backend)
        encryptor = cipher.encryptor()
        plaintext = _pad_string(plaintext)
        encrypted_text = encryptor.update(plaintext) + encryptor.finalize()
        text = binascii.b2a_hex(encrypted_text).rstrip()
    return text


def decrypt(key, iv, encrypted_text):
    backend = default_backend()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=backend)
    encrypted_text_bytes = binascii.a2b_hex(encrypted_text)
    decryptor = cipher.decryptor()
    decrypted_text = decryptor.update(encrypted_text_bytes) + decryptor.finalize()
    decrypted_text = _unpad_string(decrypted_text)
    return decrypted_text


def _pad_string(value):
    length = len(value)
    pad_size = 16 - (length % 16)
    return value.ljust(length + pad_size, b'\x00')


def _unpad_string(value):
    if value and value is not None:
        value = value.rstrip(b'\x00')
    return value


def dojo_crypto_encrypt(plaintext):
    data = None
    if plaintext:
        key = None
        key = get_db_key()

        iv = os.urandom(16)
        data = prepare_for_save(
            iv, encrypt(key, iv, plaintext.encode('utf-8')))

    return data


def prepare_for_save(iv, encrypted_value):
    stored_value = None

    if encrypted_value and encrypted_value is not None:
        binascii.b2a_hex(encrypted_value).rstrip()
        stored_value = "AES.1:" + binascii.b2a_hex(iv).decode('utf-8') + ":" + encrypted_value.decode('utf-8')
    return stored_value


def get_db_key():
    db_key = None
    if hasattr(settings, 'DB_KEY'):
        db_key = settings.DB_KEY
        db_key = binascii.b2a_hex(
            hashlib.sha256(db_key.encode('utf-8')).digest().rstrip())[:32]

    return db_key


def prepare_for_view(encrypted_value):

    key = None
    decrypted_value = ""
    if encrypted_value is not NotImplementedError and encrypted_value is not None:
        key = get_db_key()
        encrypted_values = encrypted_value.split(":")

        if len(encrypted_values) > 1:
            type = encrypted_values[0]

            iv = binascii.a2b_hex(encrypted_values[1])
            value = encrypted_values[2]

            try:
                decrypted_value = decrypt(key, iv, value)
                decrypted_value = decrypted_value.decode('utf-8')
            except UnicodeDecodeError:
                decrypted_value = ""

    return decrypted_value


def get_system_setting(setting):
    system_settings = System_Settings.objects.get()
    return getattr(system_settings, setting, None)


def get_slack_user_id(user_email):
    user_id = None

    res = requests.request(
        method='POST',
        url='https://slack.com/api/users.list',
        data={'token': get_system_setting('slack_token')})

    users = json.loads(res.text)

    slack_user_is_found = False
    if users:
        if 'error' in users:
            logger.error("Slack is complaining. See error message below.")
            logger.error(users)
        else:
            for member in users["members"]:
                if "email" in member["profile"]:
                    if user_email == member["profile"]["email"]:
                        if "id" in member:
                            user_id = member["id"]
                            logger.debug("Slack user ID is {}".format(user_id))
                            slack_user_is_found = True
                            break
                    else:
                        logger.warn("A user with email {} could not be found in this Slack workspace.".format(user_email))

            if not slack_user_is_found:
                logger.warn("The Slack user was not found.")

    return user_id


def calculate_grade(product):
    system_settings = System_Settings.objects.get()
    if system_settings.enable_product_grade:
        severity_values = Finding.objects.filter(
            ~Q(severity='Info'),
            active=True,
            duplicate=False,
            verified=True,
            false_p=False,
            test__engagement__product=product).values('severity').annotate(
                Count('numerical_severity')).order_by()

        low = 0
        medium = 0
        high = 0
        critical = 0
        for severity_count in severity_values:
            if severity_count['severity'] == "Critical":
                critical = severity_count['numerical_severity__count']
            elif severity_count['severity'] == "High":
                high = severity_count['numerical_severity__count']
            elif severity_count['severity'] == "Medium":
                medium = severity_count['numerical_severity__count']
            elif severity_count['severity'] == "Low":
                low = severity_count['numerical_severity__count']
        aeval = Interpreter()
        aeval(system_settings.product_grade)
        grade_product = "grade_product(%s, %s, %s, %s)" % (
            critical, high, medium, low)
        product.prod_numeric_grade = aeval(grade_product)
        product.save()


def get_celery_worker_status():
    from .tasks import celery_status
    res = celery_status.apply_async()

    # Wait 15 seconds for a response from Celery
    try:
        return res.get(timeout=15)
    except:
        return False


# Used to display the counts and enabled tabs in the product view
class Product_Tab():
    def __init__(self, product_id, title=None, tab=None):
        self.product = Product.objects.get(id=product_id)
        self.title = title
        self.tab = tab
        self.engagement_count = Engagement.objects.filter(
            product=self.product, active=True).count()
        self.open_findings_count = Finding.objects.filter(test__engagement__product=self.product,
                                                          false_p=False,
                                                          verified=True,
                                                          duplicate=False,
                                                          out_of_scope=False,
                                                          active=True,
                                                          mitigated__isnull=True).count()
        self.endpoints_count = Endpoint.objects.filter(
            product=self.product).count()
        self.benchmark_type = Benchmark_Type.objects.filter(
            enabled=True).order_by('name')
        self.engagement = None

    def setTab(self, tab):
        self.tab = tab

    def setEngagement(self, engagement):
        self.engagement = engagement

    def engagement(self):
        return self.engagement

    def tab(self):
        return self.tab

    def setTitle(self, title):
        self.title = title

    def title(self):
        return self.title

    def product(self):
        return self.product

    def engagements(self):
        return self.engagement_count

    def findings(self):
        return self.open_findings_count

    def endpoints(self):
        return self.endpoints_count

    def benchmark_type(self):
        return self.benchmark_type


# Used to display the counts and enabled tabs in the product view
def tab_view_count(product_id):
    product = Product.objects.get(id=product_id)
    engagements = Engagement.objects.filter(
        product=product, active=True).count()
    open_findings = Finding.objects.filter(test__engagement__product=product,
                                           false_p=False,
                                           verified=True,
                                           duplicate=False,
                                           out_of_scope=False,
                                           active=True,
                                           mitigated__isnull=True).count()
    endpoints = Endpoint.objects.filter(product=product).count()
    # benchmarks = Benchmark_Product_Summary.objects.filter(product=product, publish=True, benchmark_type__enabled=True).order_by('benchmark_type__name')
    benchmark_type = Benchmark_Type.objects.filter(
        enabled=True).order_by('name')
    return product, engagements, open_findings, endpoints, benchmark_type


# Add a lanaguage to product
def add_language(product, language):
    prod_language = Languages.objects.filter(
        language__language__iexact=language, product=product)

    if not prod_language:
        try:
            language_type = Language_Type.objects.get(
                language__iexact=language)

            if language_type:
                lang = Languages(language=language_type, product=product)
                lang.save()
        except Language_Type.DoesNotExist:
            pass


# Apply finding template data by matching CWE + Title or CWE
def apply_cwe_to_template(finding, override=False):
    if System_Settings.objects.get().enable_template_match or override:
        # Attempt to match on CWE and Title First
        template = Finding_Template.objects.filter(
            cwe=finding.cwe, title__icontains=finding.title, template_match=True).first()

        # If none then match on CWE
        template = Finding_Template.objects.filter(
            cwe=finding.cwe, template_match=True).first()

        if template:
            finding.mitigation = template.mitigation
            finding.impact = template.impact
            finding.references = template.references
            template.last_used = timezone.now()
            template.save()

    return finding


def truncate_with_dots(the_string, max_length_including_dots):
    if not the_string:
        return the_string
    return (the_string[:max_length_including_dots - 3] + '...' if len(the_string) > max_length_including_dots else the_string)


def max_safe(list):
    return max(i for i in list if i is not None)


def get_full_url(relative_url):
    if settings.SITE_URL:
        return settings.SITE_URL + relative_url
    else:
        logger.warn('SITE URL undefined in settings, full_url cannot be created')
        return "settings.SITE_URL" + relative_url


def get_site_url():
    if settings.SITE_URL:
        return settings.SITE_URL
    else:
        logger.warn('SITE URL undefined in settings, full_url cannot be created')
        return "settings.SITE_URL"


@receiver(post_save, sender=Dojo_User)
def set_default_notifications(sender, instance, created, **kwargs):
    # for new user we create a Notifications object so the default 'alert' notifications work
    # this needs to be a signal to make it also work for users created via ldap, oauth and other authentication backends
    if created:
        logger.info('creating default set of notifications for: ' + str(instance))
        notifications = Notifications()
        notifications.user = instance
        notifications.save()


@receiver(post_save, sender=Engagement)
def engagement_post_Save(sender, instance, created, **kwargs):
    if created:
        engagement = instance
        title = 'Engagement created for ' + str(engagement.product) + ': ' + str(engagement.name)
        create_notification(event='engagement_added', title=title, engagement=engagement, product=engagement.product,
                            url=reverse('view_engagement', args=(engagement.id,)))


def merge_sets_safe(set1, set2):
    return set(itertools.chain(set1 or [], set2 or []))
    # This concat looks  better, but requires Python 3.6+
    # return {*set1, *set2}


def get_return_url(request):
    return_url = request.POST.get('return_url', None)
    # print('return_url from POST: ', return_url)
    if return_url is None or not return_url.strip():
        # for some reason using request.GET.get('return_url') never works
        return_url = request.GET['return_url'] if 'return_url' in request.GET else None
        # print('return_url from GET: ', return_url)

    return return_url if return_url else None


def redirect_to_return_url_or_else(request, or_else):
    return_url = get_return_url(request)
    if return_url:
        return HttpResponseRedirect(return_url.strip())
    elif or_else:
        return HttpResponseRedirect(or_else)
    else:
        messages.add_message(request, messages.ERROR, 'Unable to redirect anywhere.', extra_tags='alert-danger')
        return HttpResponseRedirect(request.get_full_path())


def file_size_mb(file_obj):
    if file_obj:
        file_obj.seek(0, 2)
        size = file_obj.tell()
        file_obj.seek(0, 0)
        if size > 0:
            return size / 1048576
    return 0


def is_scan_file_too_large(scan_file):
    if hasattr(settings, "SCAN_FILE_MAX_SIZE"):
        size = file_size_mb(scan_file)
        if size > settings.SCAN_FILE_MAX_SIZE:
            return True
    return False
