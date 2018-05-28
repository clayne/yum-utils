# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Library General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
# by Panu Matilainen <pmatilai@laiskiainen.org>
# tweaks by James Antill <james@and.org>
#

from yum.plugins import PluginYumExit
from yum.plugins import TYPE_CORE
from rpmUtils.miscutils import splitFilename
from yum.packageSack import packagesNewestByName

import urlgrabber
import urlgrabber.grabber

import os
import fnmatch
import tempfile
import time

requires_api_version = '2.1'
plugin_type = (TYPE_CORE,)

_version_lock_excluder_n      = set()
_version_lock_excluder_nevr   = set()

_version_lock_excluder_B_nevr = set()

#  In theory we could do full nevra/pkgtup ... but having foo-1.i386 and not
# foo-1.x86_64 would be pretty weird. So just do "archless".
# _version_lock_excluder_pkgtup = set()

fileurl = None
show_hint = True
follow_obsoletes = False
no_exclude = False

def _read_locklist():
    locklist = []
    try:
        llfile = urlgrabber.urlopen(fileurl)
        for line in llfile.readlines():
            if line.startswith('#') or line.strip() == '':
                continue
            locklist.append(line.rstrip())
        llfile.close()
    except urlgrabber.grabber.URLGrabError, e:
        raise PluginYumExit('Unable to read version lock configuration: %s' % e)
    return locklist

def _match(ent, patterns):
    # there should be an API for this in Yum
    (n, v, r, e, a) = splitFilename(ent)
    for name in (
        '%s' % n,
        '%s.%s' % (n, a),
        '%s-%s' % (n, v),
        '%s-%s-%s' % (n, v, r),
        '%s-%s-%s.%s' % (n, v, r, a),
        '%s:%s-%s-%s.%s' % (e, n, v, r, a),
        '%s-%s:%s-%s.%s' % (n, e, v, r, a),
    ):
        for pat in patterns:
            if fnmatch.fnmatch(name, pat):
                return True
    return False

def _get_updates(base):
    """Return packages that update or obsolete anything in our locklist.

    Returns a dict of locked_name->X, where X is either a package object or a
    list of them.  If it's the former, it's the updating package.  If it's the
    latter, it's the obsoleting packages (since multiple packages may obsolete
    the same name).
    """

    updates = {}

    # Read in the locked versions
    locks = {}
    for ent in _read_locklist():
        (n, v, r, e, a) = splitFilename(ent)
        if e and e[0] == '!':
            e = e[1:]
        elif e == '':
            e = '0'
        locks.setdefault(n, []).append((e, v, r))

    # Process regular updates
    #
    # We are using searchNames() + packagesNewestByName() here instead of just
    # returnNewestByName() because the former way is much, much faster for big
    # name lists.
    #
    # The problem with returnNewestByName() is that it may easily end up
    # querying all the packages in pkgSack which is terribly slow (takes
    # seconds); all it takes is a "-" in a package name and more than
    # PATTERNS_MAX (8 by default) package names to trigger that.
    #
    # Since we know that we only ever deal with names, we can just go straight
    # to searchNames() to avoid the full query.
    pkgs = base.pkgSack.searchNames(locks.keys())
    for p in packagesNewestByName(pkgs):
        name = p.name
        evr = p.returnEVR()
        if (evr.epoch, evr.version, evr.release) in locks[name]:
            # This one is either the locked or excluded version, skip
            continue
        updates[name] = p

    # Process obsoletes
    tups = base.up.getObsoletesTuples() if follow_obsoletes else []
    for new, old in tups:
        nname = new[0]
        oname = old[0]
        if oname not in locks:
            # Not our package, skip
            continue
        if nname in locks and new[2:] in locks[nname]:
            # This one is either the locked or excluded version, skip
            continue
        # Only record obsoletes for any given package name
        if oname not in updates or not isinstance(updates[oname], list):
            updates[oname] = []
        p = base.getPackageObject(new)
        updates[oname].append(p)

    return updates

class VersionLockCommand:
    created = 1247693044

    def getNames(self):
        return ["versionlock"]

    def getUsage(self):
        return '[add|exclude|list|status|delete|clear] [PACKAGE-wildcard]'

    def getSummary(self):
        return 'Control package version locks.'

    def doCheck(self, base, basecmd, extcmds):
        pass

    def doCommand(self, base, basecmd, extcmds):
        cmd = 'list'
        if extcmds:
            if extcmds[0] not in ('add',
                                  'exclude', 'add-!', 'add!', 'blacklist',
                                  'list', 'status', 'del', 'delete', 'clear'):
                cmd = 'add'
            else:
                cmd = {'del'       : 'delete',
                       'add-!'     : 'exclude',
                       'add!'      : 'exclude',
                       'blacklist' : 'exclude',
                       }.get(extcmds[0], extcmds[0])
                extcmds = extcmds[1:]

        filename = fileurl
        if fileurl.startswith("file:"):
            filename = fileurl[len("file:"):]

        if not filename.startswith('/') and cmd != 'list':
            print "Error: versionlock URL isn't local: " + fileurl
            return 1, ["versionlock %s failed" % (cmd,)]

        if cmd == 'add':
            pkgs = base.rpmdb.returnPackages(patterns=extcmds)
            if not pkgs:
                pkgs = base.pkgSack.returnPackages(patterns=extcmds)

            done = set()
            for ent in _read_locklist():
                (n, v, r, e, a) = splitFilename(ent)
                done.add((n, a, e, v, r))

            fo = open(filename, 'a')
            count = 0
            for pkg in pkgs:
                #  We ignore arch, so only add one entry for foo-1.i386 and
                # foo-1.x86_64.
                (n, a, e, v, r) = pkg.pkgtup
                a = '*'
                if (n, a, e, v, r) in done:
                    continue
                done.add((n, a, e, v, r))

                print "Adding versionlock on: %s:%s-%s-%s" % (e, n, v, r)
                if not count:
                    fo.write("\n# Added locks on %s\n" % time.ctime())
                count += 1
                (n, a, e, v, r) = pkg.pkgtup
                fo.write("%s:%s-%s-%s.%s\n" % (e, n, v, r, '*'))

            return 0, ['versionlock added: ' + str(count)]

        if cmd == 'exclude':
            pkgs = base.pkgSack.returnPackages(patterns=extcmds)
            pkgs = packagesNewestByName(pkgs)

            fo = open(filename, 'a')
            count = 0
            done = set()
            for pkg in pkgs:
                #  We ignore arch, so only add one entry for foo-1.i386 and
                # foo-1.x86_64.
                (n, a, e, v, r) = pkg.pkgtup
                a = '*'
                if (n, a, e, v, r) in done:
                    continue
                done.add((n, a, e, v, r))

                print "Adding exclude on: %s:%s-%s-%s" % (e,n,v,r)
                if not count:
                    fo.write("\n# Added excludes on %s\n" % time.ctime())
                count += 1
                (n, a, e, v, r) = pkg.pkgtup
                fo.write("!%s:%s-%s-%s.%s\n" % (e, n, v, r, '*'))

            return 0, ['versionlock added: ' + str(count)]

        if cmd == 'clear':
            open(filename, 'w')
            return 0, ['versionlock cleared']

        if cmd == 'delete':
            dirname = os.path.dirname(filename)
            (out, tmpfilename) = tempfile.mkstemp(dir=dirname, suffix='.tmp')
            out = os.fdopen(out, 'w', -1)
            count = 0
            for ent in _read_locklist():
                if _match(ent, extcmds):
                    print "Deleting versionlock for:", ent
                    count += 1
                    continue
                out.write(ent)
                out.write('\n')
            out.close()
            if not count:
                os.unlink(tmpfilename)
                return 1, ['Error: versionlock delete: no matches']
            os.chmod(tmpfilename, 0644)
            os.rename(tmpfilename, filename)
            return 0, ['versionlock deleted: ' + str(count)]

        if cmd == 'status':
            global no_exclude
            no_exclude = True
            updates = _get_updates(base)
            for name, value in updates.iteritems():
                if isinstance(value, list):
                    value = set(p.envr + '.*' for p in value)
                    for v in value:
                        print '%s (replacing %s)' % (v, name)
                    continue
                print value.envr + '.*'
            return 0, ['versionlock status done']

        assert cmd == 'list'
        for ent in _read_locklist():
            print ent

        return 0, ['versionlock list done']

    def needTs(self, base, basecmd, extcmds):
        return False

def config_hook(conduit):
    global fileurl
    global follow_obsoletes
    global show_hint

    fileurl = conduit.confString('main', 'locklist')
    follow_obsoletes = conduit.confBool('main', 'follow_obsoletes', default=False)
    show_hint = conduit.confBool('main', 'show_hint', default=True)

    if hasattr(conduit._base, 'registerCommand'):
        conduit.registerCommand(VersionLockCommand())

def _add_versionlock_whitelist(conduit):
    if hasattr(conduit, 'registerPackageName'):
        conduit.registerPackageName("yum-plugin-versionlock")
    ape = conduit._base.pkgSack.addPackageExcluder
    exid = 'yum-utils.versionlock.W.'
    ape(None, exid + str(1), 'wash.marked')
    ape(None, exid + str(2), 'mark.name.in', _version_lock_excluder_n)
    ape(None, exid + str(3), 'wash.nevr.in', _version_lock_excluder_nevr)
    ape(None, exid + str(4), 'exclude.marked')

def _add_versionlock_blacklist(conduit):
    if hasattr(conduit, 'registerPackageName'):
        conduit.registerPackageName("yum-plugin-versionlock")
    ape = conduit._base.pkgSack.addPackageExcluder
    exid = 'yum-utils.versionlock.B.'
    ape(None, exid + str(1), 'wash.marked')
    ape(None, exid + str(2), 'mark.nevr.in', _version_lock_excluder_B_nevr)
    ape(None, exid + str(3), 'exclude.marked')

def exclude_hook(conduit):
    if no_exclude:
        return

    conduit.info(3, 'Reading version lock configuration')

    if not fileurl:
        raise PluginYumExit('Locklist not set')

    for ent in _read_locklist():
        neg = False
        if ent and ent[0] == '!':
            ent = ent[1:]
            neg = True
        (n, v, r, e, a) = splitFilename(ent)
        n = n.lower()
        v = v.lower()
        r = r.lower()
        e = e.lower()
        if e == '': 
            e = '0'
        if neg:
            _version_lock_excluder_B_nevr.add("%s-%s:%s-%s" % (n, e, v, r))
            continue
        _version_lock_excluder_n.add(n)
        _version_lock_excluder_nevr.add("%s-%s:%s-%s" % (n, e, v, r))

    if (_version_lock_excluder_n and follow_obsoletes):
        #  If anything obsoletes something that we have versionlocked ... then
        # remove all traces of that too.
        for (pkgtup, instTup) in conduit._base.up.getObsoletesTuples():
            if instTup[0] not in _version_lock_excluder_n:
                continue
            _version_lock_excluder_n.add(pkgtup[0].lower())

    total = len(_get_updates(conduit._base)) if show_hint else 0
    if total:
        if total > 1:
            suffix = 's'
            what = 'them'
        else:
            suffix = ''
            what = 'it'
        conduit.info(2, 'Excluding %d update%s due to versionlock '
                        '(use "yum versionlock status" to show %s)'
                        % (total, suffix, what))

    if _version_lock_excluder_n:
        _add_versionlock_whitelist(conduit)
    if _version_lock_excluder_B_nevr:
        _add_versionlock_blacklist(conduit)
