#!/usr/bin/python -tt

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
# (c) pmatilai@laiskiainen.org


import sys
import signal
import re
import fnmatch
import time
import os

from optparse import OptionParser

import yum
import yum.config

version = "0.0.6"

flags = { 'EQ':'=', 'LT':'<', 'LE':'<=', 'GT':'>', 'GE':'>=', 'None':' '}

std_qf = { 
'nvr': '%{name}-%{version}-%{release}',
'nevra': '%{name}-%{epoch}:%{version}-%{release}.%{arch}',
'info': """
Name        : %{name}
Version     : %{version}
Release     : %{release}
Architecture: %{arch}
Size        : %{installedsize}
Packager    : %{packager}
Group       : %{group}
URL         : %{url}
Repository  : %{repoid}
Summary     : %{summary}
Description :\n%{description}""",
}

def rpmevr(e, v, r):
    et = ""
    vt = ""
    rt = ""
    if e and e != "0":
        et = "%s:" % e
    if v:
        vt = "%s" % v
    if r:
        rt = "-%s" % r
    return "%s%s%s" % (et, vt, rt)

# Runtime extensions to YumAvailablePackage class
def pkgGetitem(self, item):
    res = None
    try:
        res = self.returnSimple(item)
    except KeyError:
        if item == "license":
            res = " ".join(self.licenses)
    return res

def pkgDoQuery(self, method, *args, **kw):
    if std_qf.has_key(method):
        self.qf = std_qf[method]
        return self.queryformat()
    else:
        return getattr(self, method)(*args, **kw)

def pkgPrco(self, what, **kw):
    rpdict = {}
    for rptup in self.returnPrco(what):
        (rpn, rpf, (rp,rpv,rpr)) = rptup
        # rpmlib deps should be handled on their own
        if rpn[:6] == 'rpmlib':
            continue
        if rpf:
            rpdict["%s %s %s" % (rpn, flags[rpf], rpmevr(rp,rpv,rpr))] = None
        else:
            rpdict[rpn] = None
    return rpdict.keys()

# these return formatted strings, not lists..
def fmtQueryformat(self):

    if not self.qf:
        qf = std_qf["nevra"]
    else:
        qf = self.qf

    qf = qf.replace("\\n", "\n")
    pattern = re.compile('%{(\w*?)}')
    fmt = re.sub(pattern, r'%(\1)s', qf)
    return fmt % self

def fmtList(self, **kw):
    fdict = {}
    for file in self.returnFileEntries():
        fdict[file] = None
    files = fdict.keys()
    files.sort()
    return "\n".join(files)

def fmtChangelog(self, **kw):
    changelog = []
    for date, author, message in self.returnChangelog():
        changelog.append("* %s %s\n%s\n" % (time.ctime(int(date)), author, message))
    return "\n".join(changelog)

def fmtObsoletes(self, **kw):
    return "\n".join(self._prco("obsoletes"))

def fmtProvides(self, **kw):
    return "\n".join(self._prco("provides", **kw))

def fmtRequires(self, **kw):
    return "\n".join(self._prco("requires", **kw))

def fmtConflicts(self, **kw):
    return "\n".join(self._prco("conflicts", **kw))

class groupQuery:
    def __init__(self, groupinfo, name, grouppkgs="required"):
        self.groupInfo = groupinfo
        self.grouppkgs = grouppkgs
        self.name = name
        self.group = groupinfo.group_by_id[name]

    def doQuery(self, method, *args, **kw):
        return "\n".join(getattr(self, method)(*args, **kw))

    def nevra(self):
        return ["%s - %s" % (self.group.id, self.group.name)]

    def list(self):
        pkgs = []
        for t in self.grouppkgs.split(','):
            if t == "required":
                pkgs.extend(self.groupInfo.requiredPkgs(self.name))
            elif t == "mandatory":
                pkgs.extend(self.groupInfo.mandatory_pkgs[self.name])
            elif t == "default":
                pkgs.extend(self.groupInfo.default_pkgs[self.name])
            elif t == "optional":
                pkgs.extend(self.groupInfo.optional_pkgs[self.name])
            elif t == "all":
                pkgs.extend(self.groupInfo.allPkgs(self.name))
            else:
                raise "Unknown group package type %s" % t
            
        return pkgs
        
    def requires(self):
        return self.groupInfo.requiredGroups(self.name)

    def info(self):
        return ["%s:\n\n%s\n" % (self.group.name, self.group.description)]

class YumBaseQuery(yum.YumBase):
    def __init__(self, pkgops = [], sackops = [], options = None):
        yum.YumBase.__init__(self)
        self.conf = yum.config.yumconf()
        self.options = options
        self.pkgops = pkgops
        self.sackops = sackops

    def extendPkgClass(self):
        setattr(self.pkgSack.pc, "__getitem__", pkgGetitem)
        setattr(self.pkgSack.pc, "__str__", fmtQueryformat)
        setattr(self.pkgSack.pc, "__repr__", fmtQueryformat)
        setattr(self.pkgSack.pc, "_prco", pkgPrco)
        setattr(self.pkgSack.pc, "doQuery", pkgDoQuery)
        setattr(self.pkgSack.pc, "queryformat", fmtQueryformat)
        setattr(self.pkgSack.pc, "list", fmtList)
        setattr(self.pkgSack.pc, "requires", fmtRequires)
        setattr(self.pkgSack.pc, "provides", fmtProvides)
        setattr(self.pkgSack.pc, "conflicts", fmtConflicts)
        setattr(self.pkgSack.pc, "obsoletes", fmtObsoletes)
        # XXX there's already "changelog" attribute in pkg class
        setattr(self.pkgSack.pc, "xchangelog", fmtChangelog)

        qf = self.options.queryformat or std_qf["nevra"]
        setattr(self.pkgSack.pc, "qf", qf)

    # dont log anything..
    def log(self, level, message):
        pass

    def returnItems(self):
        if self.options.group:
            grps = []
            for name in self.groupInfo.grouplist:
                grp = groupQuery(self.groupInfo, name, 
                                 grouppkgs = self.options.grouppkgs)
                grps.append(grp)
            return grps
        else:
            return self.pkgSack.returnNewestByNameArch()

    def matchPkgs(self, regexs):
        if not regexs:
            return self.returnItems()
    
        pkgs = []
        for pkg in self.returnItems():
            for expr in regexs:
                if pkg.name == expr or fnmatch.fnmatch("%s" % pkg, expr):
                    pkgs.append(pkg)

        return pkgs

    def runQuery(self, items):
        pkgs = self.matchPkgs(items)
        for pkg in pkgs:
            for oper in self.pkgops:
                print pkg.doQuery(oper)
        for prco in items:
            for oper in self.sackops:
                for p in self.doQuery(oper, prco): print p

    def doQuery(self, method, *args, **kw):
        return getattr(self, method)(*args, **kw)

    def groupmember(self, name, **kw):
        grps = []
        for id in self.groupInfo.grouplist:
            if name in self.groupInfo.allPkgs(id):
                grps.append(id)
        return grps

    def whatprovides(self, name, **kw):
        return [self.returnPackageByDep(name)]

    def whatrequires(self, name, **kw):
        pkgs = []
        provs = [name]
                
        if self.options.alldeps:
            for pkg in self.pkgSack.returnNewestByName(name):
                provs.extend(pkg._prco("provides"))

        for prov in provs:
            for pkg in self.pkgSack.searchRequires(prov):
                pkgs.append(pkg)
        return pkgs

    def requires(self, name, **kw):
        pkgs = {}
        
        for pkg in self.pkgSack.returnNewestByName(name):
            for req in pkg._prco("requires"):
                for res in self.whatprovides(req):
                    pkgs[res.name] = res
        return pkgs.values()

    def location(self, name):
        loc = []
        for pkg in self.pkgSack.returnNewestByName(name):
            repo = self.repos.getRepo(pkg.simple['repoid'])
            loc.append("%s/%s" % (repo.urls[0], pkg['relativepath']))
        return loc

def main(args):

    needfiles = 0
    needother = 0
    needgroup = 0

    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    parser = OptionParser()
    # query options
    parser.add_option("-l", "--list", default=0, action="store_true",
                      help="list files in this package/group")
    parser.add_option("-i", "--info", default=0, action="store_true",
                      help="list descriptive info from this package/group")
    parser.add_option("-f", "--file", default=0, action="store_true",
                      help="query which package provides this file")
    parser.add_option("--qf", "--queryformat", dest="queryformat",
                      help="specify a custom output format for queries")
    parser.add_option("--groupmember", default=0, action="store_true",
                      help="list which group(s) this package belongs to")
    # dummy for rpmq compatibility
    parser.add_option("-q", "--query", default=0, action="store_true",
                      help="no-op for rpmquery compatibility")
    parser.add_option("-a", "--all", default=0, action="store_true",
                      help="query all packages/groups")
    parser.add_option("--requires", default=0, action="store_true",
                      help="list package dependencies")
    parser.add_option("--provides", default=0, action="store_true",
                      help="list capabilities this package provides")
    parser.add_option("--obsoletes", default=0, action="store_true",
                      help="list other packages obsoleted by this package")
    parser.add_option("--conflicts", default=0, action="store_true",
                      help="list capabilities this package conflicts with")
    parser.add_option("--changelog", default=0, action="store_true",
                      help="show changelog for this package")
    parser.add_option("--location", default=0, action="store_true",
                      help="show download URL for this package")
    parser.add_option("--nevra", default=0, action="store_true",
                      help="show name, epoch, version, release, architecture info of package")
    parser.add_option("--nvr", default=0, action="store_true",
                      help="show name, version, release info of package")
    parser.add_option("-s", "--source", default=0, action="store_true",
                      help="show package source RPM name")
    parser.add_option("--resolve", default=0, action="store_true",
                      help="resolve capabilities to originating package(s)")
    parser.add_option("--alldeps", default=0, action="store_true",
                      help="check non-explicit dependencies as well")
    parser.add_option("--whatprovides", default=0, action="store_true",
                      help="query what package(s) provide a capability")
    parser.add_option("--whatrequires", default=0, action="store_true",
                      help="query what package(s) require a capability")
    # group stuff
    parser.add_option("--group", default=0, action="store_true", 
                      help="query groups instead of packages")
    parser.add_option("--grouppkgs", default="required", dest="grouppkgs",
                      help="filter which packages (all,optional etc) are shown from groups")
    # other opts
    parser.add_option("", "--repoid", default=[], action="append")
    parser.add_option("-v", "--version", default=0, action="store_true",
                      help="show program version and exit")

    (opts, regexs) = parser.parse_args()
    if opts.version:
        print "Repoquery version %s" % version
        sys.exit(0)

    if len(regexs) < 1 and not opts.all:
        parser.print_help()
        sys.exit(0)

    pkgops = []
    sackops = []
    archlist = None
    if opts.queryformat:
        pkgops.append("queryformat")
    if opts.requires:
        if opts.resolve:
            sackops.append("requires")
        else:
            pkgops.append("requires")
    if opts.provides:
        pkgops.append("provides")
    if opts.obsoletes:
        pkgops.append("obsoletes")
    if opts.conflicts:
        pkgops.append("conflicts")
    if opts.changelog:
        needother = 1
        pkgops.append("xchangelog")
    if opts.list:
        if not opts.group:
            needfiles = 1
        pkgops.append("list")
    if opts.info:
        pkgops.append("info")
    if opts.nvr:
        pkgops.append("nvr")
    if opts.source:
        archlist = ['src']
    if opts.whatrequires:
        sackops.append("whatrequires")
    if opts.whatprovides:
        sackops.append("whatprovides")
    if opts.file:
        sackops.append("whatprovides")
    if opts.location:
        sackops.append("location")
    if opts.groupmember:
        sackops.append("groupmember")
        needgroup = 1
    if opts.group:
        needgroup = 1

    if opts.nevra or (len(pkgops) == 0 and len(sackops) == 0):
        pkgops.append("nevra")

    repoq = YumBaseQuery(pkgops, sackops, opts)
    repoq.doConfigSetup()
    
    if os.geteuid() != 0:
        repoq.conf.setConfigOption('cache', 1)
        repoq.errorlog(0, 'Not running as root, might not be able to import all of cache.')
    
    if len(opts.repoid) > 0:
        for repo in repoq.repos.findRepos('*'):
            if repo.id not in opts.repoid:
                repo.disable()
            else:
                repo.enable()

    repoq.doRepoSetup()
    
    repoq.doSackSetup(archlist=archlist)
    if needfiles:
        repoq.repos.populateSack(with='filelists')
    if needother:
        repoq.repos.populateSack(with='otherdata')
    if needgroup:
        repoq.doTsSetup()
        repoq.doGroupSetup()

    repoq.extendPkgClass()
    repoq.runQuery(regexs)

if __name__ == "__main__":
    main(sys.argv)
                
# vim:sw=4:sts=4:expandtab              
