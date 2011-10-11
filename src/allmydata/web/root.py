import time

from twisted.internet import address
from twisted.web import http
from nevow import rend, url, loaders, tags as T
from nevow.inevow import IRequest
from nevow.static import File as nevow_File # TODO: merge with static.File?
from nevow.util import resource_filename
from formless import webform

import allmydata # to display import path
from allmydata import get_package_versions_string
from allmydata import provisioning
from allmydata.util import idlib, log
from allmydata.interfaces import IFileNode
from allmydata.web import filenode, directory, unlinked, status, operations
from allmydata.web import reliability, storage
from allmydata.web.common import abbreviate_size, getxmlfile, WebError, \
     get_arg, RenderMixin, get_format, get_mutable_type


class URIHandler(RenderMixin, rend.Page):
    # I live at /uri . There are several operations defined on /uri itself,
    # mostly involved with creation of unlinked files and directories.

    def __init__(self, client):
        rend.Page.__init__(self, client)
        self.client = client

    def render_GET(self, ctx):
        req = IRequest(ctx)
        uri = get_arg(req, "uri", None)
        if uri is None:
            raise WebError("GET /uri requires uri=")
        there = url.URL.fromContext(ctx)
        there = there.clear("uri")
        # I thought about escaping the childcap that we attach to the URL
        # here, but it seems that nevow does that for us.
        there = there.child(uri)
        return there

    def render_PUT(self, ctx):
        req = IRequest(ctx)
        # either "PUT /uri" to create an unlinked file, or
        # "PUT /uri?t=mkdir" to create an unlinked directory
        t = get_arg(req, "t", "").strip()
        if t == "":
            file_format = get_format(req, "CHK")
            mutable_type = get_mutable_type(file_format)
            if mutable_type is not None:
                return unlinked.PUTUnlinkedSSK(req, self.client, mutable_type)
            else:
                return unlinked.PUTUnlinkedCHK(req, self.client)
        if t == "mkdir":
            return unlinked.PUTUnlinkedCreateDirectory(req, self.client)
        errmsg = ("/uri accepts only PUT, PUT?t=mkdir, POST?t=upload, "
                  "and POST?t=mkdir")
        raise WebError(errmsg, http.BAD_REQUEST)

    def render_POST(self, ctx):
        # "POST /uri?t=upload&file=newfile" to upload an
        # unlinked file or "POST /uri?t=mkdir" to create a
        # new directory
        req = IRequest(ctx)
        t = get_arg(req, "t", "").strip()
        if t in ("", "upload"):
            file_format = get_format(req)
            mutable_type = get_mutable_type(file_format)
            if mutable_type is not None:
                return unlinked.POSTUnlinkedSSK(req, self.client, mutable_type)
            else:
                return unlinked.POSTUnlinkedCHK(req, self.client)
        if t == "mkdir":
            return unlinked.POSTUnlinkedCreateDirectory(req, self.client)
        elif t == "mkdir-with-children":
            return unlinked.POSTUnlinkedCreateDirectoryWithChildren(req,
                                                                    self.client)
        elif t == "mkdir-immutable":
            return unlinked.POSTUnlinkedCreateImmutableDirectory(req,
                                                                 self.client)
        errmsg = ("/uri accepts only PUT, PUT?t=mkdir, POST?t=upload, "
                  "and POST?t=mkdir")
        raise WebError(errmsg, http.BAD_REQUEST)

    def childFactory(self, ctx, name):
        # 'name' is expected to be a URI
        try:
            node = self.client.create_node_from_uri(name)
            return directory.make_handler_for(node, self.client)
        except (TypeError, AssertionError):
            raise WebError("'%s' is not a valid file- or directory- cap"
                           % name)

class FileHandler(rend.Page):
    # I handle /file/$FILECAP[/IGNORED] , which provides a URL from which a
    # file can be downloaded correctly by tools like "wget".

    def __init__(self, client):
        rend.Page.__init__(self, client)
        self.client = client

    def childFactory(self, ctx, name):
        req = IRequest(ctx)
        if req.method not in ("GET", "HEAD"):
            raise WebError("/file can only be used with GET or HEAD")
        # 'name' must be a file URI
        try:
            node = self.client.create_node_from_uri(name)
        except (TypeError, AssertionError):
            # I think this can no longer be reached
            raise WebError("'%s' is not a valid file- or directory- cap"
                           % name)
        if not IFileNode.providedBy(node):
            raise WebError("'%s' is not a file-cap" % name)
        return filenode.FileNodeDownloadHandler(self.client, node)

    def renderHTTP(self, ctx):
        raise WebError("/file must be followed by a file-cap and a name",
                       http.NOT_FOUND)

class IncidentReporter(RenderMixin, rend.Page):
    def render_POST(self, ctx):
        req = IRequest(ctx)
        log.msg(format="User reports incident through web page: %(details)s",
                details=get_arg(req, "details", ""),
                level=log.WEIRD, umid="LkD9Pw")
        req.setHeader("content-type", "text/plain")
        return "Thank you for your report!"

class NoReliability(rend.Page):
    docFactory = loaders.xmlstr('''\
<html xmlns:n="http://nevow.com/ns/nevow/0.1">
  <head>
    <title>AllMyData - Tahoe</title>
    <link href="/webform_css" rel="stylesheet" type="text/css"/>
    <meta http-equiv="Content-Type" content="text/html; charset=utf-8" />
  </head>
  <body>
  <h2>"Reliability" page not available</h2>
  <p>Please install the python "NumPy" module to enable this page.</p>
  </body>
</html>
''')

SPACE = u"\u00A0"*2

class Root(rend.Page):

    addSlash = True
    docFactory = getxmlfile("welcome.xhtml")

    def __init__(self, client, clock=None):
        rend.Page.__init__(self, client)
        self.client = client
        # If set, clock is a twisted.internet.task.Clock that the tests
        # use to test ophandle expiration.
        self.child_operations = operations.OphandleTable(clock)
        try:
            s = client.getServiceNamed("storage")
        except KeyError:
            s = None
        self.child_storage = storage.StorageStatus(s)

        self.child_uri = URIHandler(client)
        self.child_cap = URIHandler(client)

        self.child_file = FileHandler(client)
        self.child_named = FileHandler(client)
        self.child_status = status.Status(client.get_history())
        self.child_statistics = status.Statistics(client.stats_provider)
        def f(name):
            return nevow_File(resource_filename('allmydata.web', name))
        self.putChild("download_status_timeline.js", f("download_status_timeline.js"))
        self.putChild("jquery-1.6.1.min.js", f("jquery-1.6.1.min.js"))
        self.putChild("protovis-3.3.1.min.js", f("protovis-3.3.1.min.js"))

    def child_helper_status(self, ctx):
        # the Helper isn't attached until after the Tub starts, so this child
        # needs to created on each request
        return status.HelperStatus(self.client.helper)

    child_webform_css = webform.defaultCSS
    child_tahoe_css = nevow_File(resource_filename('allmydata.web', 'tahoe.css'))

    child_provisioning = provisioning.ProvisioningTool()
    if reliability.is_available():
        child_reliability = reliability.ReliabilityTool()
    else:
        child_reliability = NoReliability()

    child_report_incident = IncidentReporter()
    #child_server # let's reserve this for storage-server-over-HTTP

    # FIXME: This code is duplicated in root.py and introweb.py.
    def data_version(self, ctx, data):
        return get_package_versions_string()
    def data_import_path(self, ctx, data):
        return str(allmydata)
    def data_my_nodeid(self, ctx, data):
        return idlib.nodeid_b2a(self.client.nodeid)
    def data_my_nickname(self, ctx, data):
        return self.client.nickname

    def render_services(self, ctx, data):
        ul = T.ul()
        try:
            ss = self.client.getServiceNamed("storage")
            stats = ss.get_stats()
            if stats["storage_server.accepting_immutable_shares"]:
                msg = "accepting new shares"
            else:
                msg = "not accepting new shares (read-only)"
            available = stats.get("storage_server.disk_avail")
            if available is not None:
                msg += ", %s available" % abbreviate_size(available)
            ul[T.li[T.a(href="storage")["Storage Server"], ": ", msg]]
        except KeyError:
            ul[T.li["Not running storage server"]]

        if self.client.helper:
            stats = self.client.helper.get_stats()
            active_uploads = stats["chk_upload_helper.active_uploads"]
            ul[T.li["Helper: %d active uploads" % (active_uploads,)]]
        else:
            ul[T.li["Not running helper"]]

        return ctx.tag[ul]

    def data_introducer_furl(self, ctx, data):
        return self.client.introducer_furl
    def data_connected_to_introducer(self, ctx, data):
        if self.client.connected_to_introducer():
            return "yes"
        return "no"

    def data_helper_furl(self, ctx, data):
        try:
            uploader = self.client.getServiceNamed("uploader")
        except KeyError:
            return None
        furl, connected = uploader.get_helper_info()
        return furl
    def data_connected_to_helper(self, ctx, data):
        try:
            uploader = self.client.getServiceNamed("uploader")
        except KeyError:
            return "no" # we don't even have an Uploader
        furl, connected = uploader.get_helper_info()
        if connected:
            return "yes"
        return "no"

    def data_known_storage_servers(self, ctx, data):
        sb = self.client.get_storage_broker()
        return len(sb.get_all_serverids())

    def data_connected_storage_servers(self, ctx, data):
        sb = self.client.get_storage_broker()
        return len(sb.get_connected_servers())

    def data_services(self, ctx, data):
        sb = self.client.get_storage_broker()
        return sorted(sb.get_known_servers(), key=lambda s: s.get_nickname())

    def render_service_row(self, ctx, server):
        nodeid = server.get_serverid()

        ctx.fillSlots("peerid", server.get_longname())
        ctx.fillSlots("nickname", server.get_nickname())
        rhost = server.get_remote_host()
        if rhost:
            if nodeid == self.client.nodeid:
                rhost_s = "(loopback)"
            elif isinstance(rhost, address.IPv4Address):
                rhost_s = "%s:%d" % (rhost.host, rhost.port)
            else:
                rhost_s = str(rhost)
            connected = "Yes: to " + rhost_s
            since = server.get_last_connect_time()
        else:
            connected = "No"
            since = server.get_last_loss_time()
        announced = server.get_announcement_time()
        announcement = server.get_announcement()
        version = announcement["my-version"]

        status = server.get_account_status()
        def _format_status(status):
            # WRS= FFF FFT FTT TTT
            if not status.get("save",True):
                return "deleted: all shares deleted"
            if not status.get("read",True):
                return "disabled: no read or write"
            if not status.get("write",True):
                return "frozen: read, but no write"
            return "normal: full read+write"
        ctx.fillSlots("status", _format_status(status))

        message = server.get_account_message()
        def _format_message(msg):
            bits = T.span()
            if "message" in msg:
                bits[msg["message"]]
            keys = set(msg.keys())
            keys.discard("message")
            if keys:
                keys = sorted(keys)
                for k in keys:
                    bits[T.br()]
                    bits["%s: %s" % (k, msg[k])]
            return bits
        ctx.fillSlots("server_message", _format_message(message))

        # consider this:
        #  cache the usage, with a timestamp
        #  if the usage is more than 5 minutes out of date:
        #    put a "?" here
        #    and send queries to update it
        #  that means get_claimed_usage() returns immediately, can return
        #  None, and fires off requests in the background.
        TIME_FORMAT = "%H:%M:%S %d-%b-%Y"

        bytes,when = server.get_claimed_usage()
        if bytes is None:
            usage = T.span(title="no data")["?"]
        else:
            when = time.strftime(TIME_FORMAT, time.localtime(when))
            usage = T.span(title="as of %s" % when)[abbreviate_size(bytes)]
        ctx.fillSlots("usage", usage)

        ctx.fillSlots("connected", connected)
        ctx.fillSlots("connected-bool", bool(rhost))
        ctx.fillSlots("since", time.strftime(TIME_FORMAT,
                                             time.localtime(since)))
        ctx.fillSlots("announced", time.strftime(TIME_FORMAT,
                                                 time.localtime(announced)))
        ctx.fillSlots("version", version)

        return ctx.tag

    def data_clients(self, ctx, data):
        a = self.client.get_accountant()
        if a:
            return sorted(a.get_all_accounts(),
                          key=lambda account: account.get_nickname())
        return []

    def render_client_row(self, ctx, account):
        c = account.get_connection_status()
        if c["connected"]:
            cs = "Yes: from %s" % c["last_connected_from"]
        else:
            # there is a window (between Account creation and our connection
            # to the 'rxFURL' receiver) during which the Account exists but
            # we've never connected to it. So c["last_connected_from"] can be
            # None.
            cs = "No: last from %s" % c["last_connected_from"]
        ctx.fillSlots("nickname", account.get_nickname())
        ctx.fillSlots("clientid", account.get_id())
        ctx.fillSlots("connected-bool", c["connected"])
        ctx.fillSlots("connected", cs)

        TIME_FORMAT = "%H:%M:%S %d-%b-%Y"
        if c["connected"]:
            since = time.strftime(TIME_FORMAT,
                                  time.localtime(c["connected_since"]))
        elif c["last_seen"]:
            since = time.strftime(TIME_FORMAT,
                                  time.localtime(c["last_seen"]))
        else:
            since = ""
        ctx.fillSlots("since", since)
        created = "?"
        if c["created"]:
            created = time.strftime(TIME_FORMAT, time.localtime(c["created"]))
        ctx.fillSlots("created", created)
        ctx.fillSlots("usage", abbreviate_size(account.get_current_usage()))
        return ctx.tag

    def render_download_form(self, ctx, data):
        # this is a form where users can download files by URI
        form = T.form(action="uri", method="get",
                      enctype="multipart/form-data")[
            T.fieldset[
            T.legend(class_="freeform-form-label")["Download a file"],
            T.div["Tahoe-URI to download:"+SPACE,
                  T.input(type="text", name="uri")],
            T.div["Filename to download as:"+SPACE,
                  T.input(type="text", name="filename")],
            T.input(type="submit", value="Download!"),
            ]]
        return T.div[form]

    def render_view_form(self, ctx, data):
        # this is a form where users can download files by URI, or jump to a
        # named directory
        form = T.form(action="uri", method="get",
                      enctype="multipart/form-data")[
            T.fieldset[
            T.legend(class_="freeform-form-label")["View a file or directory"],
            "Tahoe-URI to view:"+SPACE,
            T.input(type="text", name="uri"), SPACE*2,
            T.input(type="submit", value="View!"),
            ]]
        return T.div[form]

    def render_upload_form(self, ctx, data):
        # This is a form where users can upload unlinked files.
        # Users can choose immutable, SDMF, or MDMF from a radio button.

        upload_chk  = T.input(type='radio', name='format',
                              value='chk', id='upload-chk',
                              checked='checked')
        upload_sdmf = T.input(type='radio', name='format',
                              value='sdmf', id='upload-sdmf')
        upload_mdmf = T.input(type='radio', name='format',
                              value='mdmf', id='upload-mdmf')

        form = T.form(action="uri", method="post",
                      enctype="multipart/form-data")[
            T.fieldset[
            T.legend(class_="freeform-form-label")["Upload a file"],
            T.div["Choose a file:"+SPACE,
                  T.input(type="file", name="file", class_="freeform-input-file")],
            T.input(type="hidden", name="t", value="upload"),
            T.div[upload_chk,  T.label(for_="upload-chk") [" Immutable"],           SPACE,
                  upload_sdmf, T.label(for_="upload-sdmf")[" SDMF"],                SPACE,
                  upload_mdmf, T.label(for_="upload-mdmf")[" MDMF (experimental)"], SPACE*2,
                  T.input(type="submit", value="Upload!")],
            ]]
        return T.div[form]

    def render_mkdir_form(self, ctx, data):
        # This is a form where users can create new directories.
        # Users can choose SDMF or MDMF from a radio button.

        mkdir_sdmf = T.input(type='radio', name='format',
                             value='sdmf', id='mkdir-sdmf',
                             checked='checked')
        mkdir_mdmf = T.input(type='radio', name='format',
                             value='mdmf', id='mkdir-mdmf')

        form = T.form(action="uri", method="post",
                      enctype="multipart/form-data")[
            T.fieldset[
            T.legend(class_="freeform-form-label")["Create a directory"],
            mkdir_sdmf, T.label(for_='mkdir-sdmf')[" SDMF"],                SPACE,
            mkdir_mdmf, T.label(for_='mkdir-mdmf')[" MDMF (experimental)"], SPACE*2,
            T.input(type="hidden", name="t", value="mkdir"),
            T.input(type="hidden", name="redirect_to_result", value="true"),
            T.input(type="submit", value="Create a directory"),
            ]]
        return T.div[form]

    def render_incident_button(self, ctx, data):
        # this button triggers a foolscap-logging "incident"
        form = T.form(action="report_incident", method="post",
                      enctype="multipart/form-data")[
            T.fieldset[
            T.legend(class_="freeform-form-label")["Report an Incident"],
            T.input(type="hidden", name="t", value="report-incident"),
            "What went wrong?:"+SPACE,
            T.input(type="text", name="details"), SPACE,
            T.input(type="submit", value="Report!"),
            ]]
        return T.div[form]
