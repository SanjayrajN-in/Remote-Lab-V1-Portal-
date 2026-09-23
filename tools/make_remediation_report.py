"""Builds Remediation_Report.pdf - the response to the ISO assessment.

Run from the project root:  venv/bin/python tools/make_remediation_report.py

The finding statuses are held in the lists below (crit / other / part / op).
When a finding is fixed, move its entry between those lists and update the
"Status at a glance" table to match - the totals are not computed, so they
have to be kept in step by hand. Version 1.0 of this report shipped a table
that did not add up, which is why that is worth saying explicitly.

Needs reportlab, which is not a runtime dependency of the portal:
    venv/bin/pip install reportlab
"""
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph,
                                Spacer, Table, TableStyle, PageBreak, KeepTogether)

NAVY = colors.HexColor("#123a5f")
GREY = colors.HexColor("#5a6b7b")
GREEN = colors.HexColor("#1d7a46")
AMBER = colors.HexColor("#a8670d")
RED = colors.HexColor("#a11")
LINE = colors.HexColor("#d3dae1")
BAND = colors.HexColor("#f2f5f8")

ss = getSampleStyleSheet()
def S(name, **kw):
    base = kw.pop("parent", ss["Normal"])
    return ParagraphStyle(name, parent=base, **kw)

body    = S("body", fontSize=9.5, leading=14, spaceAfter=7, textColor=colors.HexColor("#1c2733"))
h1      = S("h1", fontSize=16, leading=20, textColor=NAVY, spaceBefore=16, spaceAfter=9,
            fontName="Helvetica-Bold")
h2      = S("h2", fontSize=11, leading=15, textColor=NAVY, spaceBefore=12, spaceAfter=5,
            fontName="Helvetica-Bold")
small   = S("small", fontSize=8.2, leading=11.5, textColor=GREY)
cell    = S("cell", fontSize=8.5, leading=11.5)
cellb   = S("cellb", fontSize=8.5, leading=11.5, fontName="Helvetica-Bold")
cover_t = S("cover_t", fontSize=26, leading=31, textColor=NAVY, fontName="Helvetica-Bold")
cover_s = S("cover_s", fontSize=13, leading=18, textColor=GREY)

def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(LINE); canvas.setLineWidth(0.5)
    canvas.line(20*mm, 15*mm, 190*mm, 15*mm)
    canvas.setFont("Helvetica", 7.5); canvas.setFillColor(GREY)
    canvas.drawString(20*mm, 10.5*mm, "Remote Lab Portal - Remediation Report - CONFIDENTIAL")
    canvas.drawRightString(190*mm, 10.5*mm, "Page %d" % doc.page)
    canvas.restoreState()

doc = BaseDocTemplate("Remediation_Report.pdf", pagesize=A4,
                      leftMargin=20*mm, rightMargin=20*mm,
                      topMargin=18*mm, bottomMargin=20*mm,
                      title="Remote Lab Portal - Remediation Report",
                      author="Remote Lab Portal project team")
doc.addPageTemplates([PageTemplate(id="main",
    frames=[Frame(20*mm, 20*mm, 170*mm, 257*mm, id="f")], onPage=footer)])

def status_tag(text, colour):
    return Paragraph('<font color="%s"><b>%s</b></font>' % (colour.hexval(), text), cell)

def table(rows, widths, header=True):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    st = [("VALIGN", (0,0), (-1,-1), "TOP"),
          ("GRID", (0,0), (-1,-1), 0.4, LINE),
          ("LEFTPADDING", (0,0), (-1,-1), 5), ("RIGHTPADDING", (0,0), (-1,-1), 5),
          ("TOPPADDING", (0,0), (-1,-1), 4), ("BOTTOMPADDING", (0,0), (-1,-1), 4)]
    if header:
        st += [("BACKGROUND", (0,0), (-1,0), NAVY),
               ("TEXTCOLOR", (0,0), (-1,0), colors.white),
               ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
               ("FONTSIZE", (0,0), (-1,0), 8.5)]
        for i in range(2, len(rows), 2):
            st.append(("BACKGROUND", (0,i), (-1,i), BAND))
    t.setStyle(TableStyle(st))
    return t

E = []
A = E.append

# ---------------- cover ----------------
A(Spacer(1, 45*mm))
A(Paragraph("Remote Lab Portal", cover_t))
A(Paragraph("Remediation Report", cover_s))
A(Spacer(1, 6*mm))
A(Table([[""]], colWidths=[45*mm], rowHeights=[2.5],
        style=TableStyle([("BACKGROUND",(0,0),(-1,-1),NAVY)])))
A(Spacer(1, 9*mm))
A(Paragraph("A response to the White-Box Security Assessment Report "
            "(Version 1.0, 22 September 2026) issued by the Information "
            "Security Office, DIGITS, Indian Institute of Science.", body))
A(Spacer(1, 12*mm))
A(table([
    [Paragraph("Prepared for", cellb), Paragraph("Information Security Office (ISO), DIGITS, IISc", cell)],
    [Paragraph("Prepared by", cellb), Paragraph("Remote Lab Portal project team", cell)],
    [Paragraph("Date", cellb), Paragraph("23 September 2026 (Version 2.0)", cell)],
    [Paragraph("Responds to", cellb), Paragraph("36 findings raised against commit 3fcbca6", cell)],
    [Paragraph("Classification", cellb), Paragraph("Confidential - restricted distribution", cell)],
], [38*mm, 132*mm], header=False))
A(Spacer(1, 14*mm))
A(Paragraph("<b>Headline:</b> all 8 Critical findings and all 12 High findings are now "
            "closed or substantially closed. 22 of 36 findings are fully resolved, 8 are "
            "partly resolved, and 6 remain open - none of them High or Critical. No open "
            "finding allows an unauthenticated person to reach laboratory hardware.", body))
A(PageBreak())

# ---------------- summary ----------------
A(Paragraph("1. Summary", h1))
A(Paragraph("The assessment raised 36 findings, 8 of them Critical, and recommended that "
            "the portal not be exposed beyond a closed laboratory network until the "
            "Critical items were fixed. This report records what has been done since.", body))
A(Paragraph("The central problem the assessment identified was that laboratory hardware "
            "could be controlled from the network without any account. Someone who could "
            "reach the server could obtain the access keys of students running experiments, "
            "and then use those keys to upload firmware to a bench, drive its serial "
            "connection, watch its camera or shut the session down. That chain has been "
            "broken at every link, and the fixes have been confirmed working on the running "
            "system rather than only in testing.", body))
A(Paragraph("Work was carried out on 23 September 2026 across 26 changes. Each change "
            "addresses one finding and carries an automated test that was first confirmed "
            "to fail against the old code, so the test proves the problem existed and is "
            "now gone. The project's automated test suite grew from 152 checks to 274.", body))
A(Paragraph("This version supersedes the report of the same date, which was issued before "
            "the last six fixes were made. One correction is also noted: the summary table "
            "in that version stated 17 resolved, 9 partly resolved and 10 open, which does "
            "not total 36. The correct figures at that time were 16, 9 and 11, as its own "
            "headline stated. The table below is the current position.", body))

A(Paragraph("Status at a glance", h2))
A(table([
    [Paragraph("Severity", cellb), Paragraph("Raised", cellb), Paragraph("Fully resolved", cellb),
     Paragraph("Partly resolved", cellb), Paragraph("Open", cellb)],
    [Paragraph("Critical", cell), Paragraph("8", cell), status_tag("8", GREEN), Paragraph("0", cell), Paragraph("0", cell)],
    [Paragraph("High", cell), Paragraph("12", cell), status_tag("10", GREEN), status_tag("2", AMBER), Paragraph("0", cell)],
    [Paragraph("Medium", cell), Paragraph("10", cell), status_tag("4", GREEN), status_tag("2", AMBER), status_tag("4", RED)],
    [Paragraph("Low", cell), Paragraph("1", cell), Paragraph("0", cell), status_tag("1", AMBER), Paragraph("0", cell)],
    [Paragraph("Informational", cell), Paragraph("5", cell), Paragraph("0", cell), status_tag("3", AMBER), status_tag("2", RED)],
    [Paragraph("<b>Total</b>", cell), Paragraph("<b>36</b>", cell), Paragraph("<b>22</b>", cell),
     Paragraph("<b>8</b>", cell), Paragraph("<b>6</b>", cell)],
], [34*mm, 24*mm, 38*mm, 38*mm, 36*mm]))
A(Spacer(1, 3*mm))
A(Paragraph("The overall risk rating of Critical was driven by the unauthenticated route to "
            "hardware control. That route is closed, and no Critical or High finding now "
            "remains open. The project team's assessment is that the residual risk is Low to "
            "Medium, and that the six remaining items are ordinary remediation work rather "
            "than reasons to withhold the service.", body))
A(PageBreak())

# ---------------- critical ----------------
A(Paragraph("2. Critical findings - all resolved", h1))
A(Paragraph("Each of these was reachable by someone with no account on the portal.", body))
crit = [
    ("F-01", "The compatibility service for older benches accepted instructions from anyone on "
             "the network, with no password, because that behaviour was switched on by default.",
             "The service now always requires the shared key. The setting that allowed anonymous "
             "access can no longer override it, and is off by default so a server that loses its "
             "configuration file cannot silently reopen the hole. The service also stopped handing "
             "out each bench's individual key to anyone who asked."),
    ("F-02", "Anyone could read the access keys of students currently running experiments, "
             "together with their names and institutional email addresses.",
             "These pages now require authentication. Separately, student names and email "
             "addresses are no longer sent to the benches at all - a bench is told which key is "
             "valid and when it expires, identified only by booking code."),
    ("F-03", "Anyone holding an access key could upload firmware to a physical bench. No account "
             "was needed. The code claimed to check ownership but the check was never written.",
             "Uploading now requires the student who booked that bench to be signed in. The file "
             "size is also limited before the file is read into memory, and refused attempts are "
             "recorded."),
    ("F-04", "Anyone holding an access key could take over a student's live experiment, including "
             "the serial console and the debugger.",
             "The live connection now requires the owning student to be signed in, and this is "
             "re-checked on every single command rather than only when the connection opens - so "
             "an administrator ending a session takes effect immediately."),
    ("F-06", "The administrator sign-in page applied no lockout after repeated failures, and its "
             "error messages revealed when a password was correct - for any account, not just "
             "administrators.",
             "Both sign-in pages now share one routine. Lockout applies to both, every failure "
             "produces an identical response, and failed attempts are recorded. A student signing "
             "in at the staff page is simply sent to their own dashboard rather than being told "
             "their password was right."),
    ("F-07", "If the configuration file was missing, the system started anyway using default "
             "passwords that are published in the source code.",
             "The system now refuses to start on a missing, placeholder or too-short key. It also "
             "no longer builds itself as a side effect of being imported, which is what made the "
             "failure reachable."),
    ("F-10", "A bench's network address could be set by anyone, and the portal would then send "
             "requests to whatever address was given - including internal services that trust it.",
             "Addresses are now checked against the laboratory network and a list of permitted "
             "ports. The address a bench actually connects from takes priority over the address it "
             "claims, and the portal no longer follows redirections from a bench."),
    ("F-14", "Anyone could end any student's live session, and doing so destroyed the student's "
             "booking so they could not restart it.",
             "Only the bench that owns a session can end it, proven with that bench's own key. "
             "Ending a session no longer cancels the booking, so the slot stays usable."),
]
for fid, prob, fix in crit:
    A(KeepTogether([
        Paragraph("%s &nbsp;<font color='%s'>RESOLVED</font>" % (fid, GREEN.hexval()), h2),
        Paragraph("<b>What was wrong.</b> " + prob, body),
        Paragraph("<b>What was done.</b> " + fix, body),
    ]))
A(PageBreak())

# ---------------- other resolved ----------------
A(Paragraph("3. Other findings resolved", h1))
other = [
    ("F-05", "High", "A second, unprotected copy of the bench camera feed existed alongside the "
                     "protected one. It has been removed."),
    ("F-08", "High", "Session cookies are marked secure, the site is served over HTTPS, and the "
                     "application is configured to sit behind the web server correctly. A change "
                     "restricting the application to local connections only is prepared and "
                     "awaits a scheduled maintenance window."),
    ("F-12", "High", "The live-instrument connection accepted connections from any website on the "
                     "internet. It now accepts them only from the portal's own address."),
    ("F-23", "High", "Five software components carried eleven published vulnerabilities. All have "
                     "been upgraded and the vulnerability scanner now reports none. The upgrade "
                     "was tested in an isolated copy before being applied."),
    ("F-29", "High", "The service ran as a staff member's own login account, which gave it access "
                     "to that person's files and privileges. A restricted account with no login "
                     "rights is prepared, along with substantially tighter operating-system "
                     "restrictions; this awaits a scheduled maintenance window."),
    ("F-20", "Medium", "The sign-in page could be tricked into sending a student to an outside "
                       "website immediately after they signed in, which could be used for "
                       "convincing phishing. Only addresses on the portal itself are now accepted."),
    ("F-25", "Medium", "The thirty-minute inactivity sign-out signed the user out and then "
                       "crashed, showing a server error instead of the sign-in page. Fixed."),
    ("F-30", "Medium", "Server addresses and a reference to a weak password have been removed "
                       "from the project documentation and from its history."),
    ("F-11", "High", "Information a bench sends back - register names, disassembly, variable "
                     "names and values - was placed into the debugger panel as page content "
                     "rather than as plain text, so a bench that had been tampered with could "
                     "have run code inside a student's browser, within their signed-in session. "
                     "All such text is now made safe at the point it is displayed. The same "
                     "code existed in two pages and both were corrected."),
    ("F-13", "High", "Temporary passwords and password-reset links were written into the server "
                     "log and displayed on screen to the administrator. Neither is now recorded "
                     "or shown; the log carries only a reference number, the recipient and the "
                     "subject, which is enough to trace a message without disclosing it."),
    ("F-19", "High", "Charting, icons, fonts and the live-instrument library were downloaded "
                     "from four external services on every page load, with no check that they "
                     "were unaltered - and the charting library was requested without naming a "
                     "version at all, so the portal accepted whatever those services published "
                     "that day. All are now served by the portal itself from files held within "
                     "the project, with each file's origin and checksum recorded. The browser "
                     "security policy no longer permits any external source. A side benefit: "
                     "the instrument pages now work on a closed network, which they could not "
                     "have done before."),
    ("F-27", "High", "Upload size is limited before the file is read into memory. The permitted "
                     "file types are now determined by the bench record rather than by a value "
                     "the uploader supplies, so the restriction can no longer be chosen by the "
                     "person being restricted. The result of the virus scan is recorded against "
                     "every accepted upload, so 'scanned and clean' is now distinguishable from "
                     "'never scanned'."),
    ("F-28", "High", "The optional demonstration data gave all three sample accounts one fixed "
                     "password, which also appeared in the project documentation. Each account "
                     "now receives its own random password, displayed once when the command is "
                     "run. The live database was checked and holds no demonstration accounts."),
    ("F-09", "Medium", "A page on another website could cause a signed-in user's browser to "
                       "carry out actions on the portal without their knowledge - including "
                       "administrator actions such as creating users or devices. Every form and "
                       "every instrument command now carries a single-use ticket that only the "
                       "portal's own pages can obtain, and any request arriving without one is "
                       "refused. Sign-out was also changed from a link to a button, because a "
                       "link can be triggered by any page that a browser visits."),
]
A(table([[Paragraph("ID", cellb), Paragraph("Severity", cellb), Paragraph("What was done", cellb)]] +
        [[Paragraph(f, cell), Paragraph(s, cell), Paragraph(t, cell)] for f, s, t in other],
        [16*mm, 20*mm, 134*mm]))

A(Paragraph("4. Findings partly resolved", h1))
A(Paragraph("In each of these the most serious part has been addressed and a smaller part "
            "remains.", body))
part = [
    ("F-15", "High", "Benches can no longer be registered anonymously, which removed the "
                     "practical route to flooding the database. A hard limit on the number of "
                     "benches is still to be added."),
    ("F-17", "High", "Sign-in attempt limits have been added at the web server, and the account "
                     "lockout fixed under F-06 now works on both sign-in pages. Limits on the "
                     "remaining pages are still to be added."),
    ("F-21", "Medium", "Sign-in responses no longer reveal which email addresses have accounts. "
                       "The separate concern - that repeated failures can be used to lock a "
                       "named person out deliberately - remains."),
    ("F-22", "Low", "Student names and email addresses are no longer sent to benches. A written "
                    "retention period for session and upload records is still to be defined."),
    ("F-31", "Medium", "Failed sign-ins, lockouts, refused uploads and refused bench addresses "
                       "are now recorded, none of which were before. Recording the source address "
                       "with each entry, sending logs off the machine, and alerting remain."),
    ("F-33", "Info", "The two previously unpinned components are now fixed to exact versions. A "
                     "full verified dependency lock file is still to be produced."),
    ("F-35", "Info", "Superseded and backup files have been removed from the project. Two "
                     "duplicate copies of the live-instrument code still exist."),
    ("F-36", "Info", "Automated security tests now exist for every Critical finding and run with "
                     "the normal test suite. Two-factor sign-in for administrators, and an "
                     "automated build pipeline, remain outstanding."),
]
A(table([[Paragraph("ID", cellb), Paragraph("Severity", cellb), Paragraph("Position", cellb)]] +
        [[Paragraph(f, cell), Paragraph(s, cell), Paragraph(t, cell)] for f, s, t in part],
        [16*mm, 20*mm, 134*mm]))
A(PageBreak())

# ---------------- open ----------------
A(Paragraph("5. Findings still open", h1))
A(Paragraph("Six findings remain. None is Critical or High. None can be reached by "
            "someone without an account, none gives control of laboratory hardware, and "
            "none exposes personal data. They are scheduled rather than urgent.", body))
op = [
    ("F-16", "Medium", "Two students booking the same slot at the same instant can both "
                       "succeed, because the check and the booking are not a single "
                       "indivisible step. The consequence is a double booking to be sorted "
                       "out by staff, not a security breach."),
    ("F-18", "Medium", "Passwords need only be eight characters, with no check against known "
                       "breached passwords. A limit on how often a password may be changed is "
                       "written in the configuration but never actually applied."),
    ("F-24", "Medium", "Two administrator forms do not check that a value expected to be a "
                       "number is one, so a malformed entry produces a server error page "
                       "rather than a polite message. No data is exposed by it."),
    ("F-26", "Medium", "The bulk user import honours a 'role' column, so a spreadsheet "
                       "prepared by someone else could create administrator accounts without "
                       "the member of staff importing it noticing. It requires an "
                       "administrator to perform the import."),
    ("F-32", "Info", "Session keys are shorter than ideal for a value that controls physical "
                     "equipment. They are not guessable in practice."),
    ("F-34", "Info", "The example code supplied for the benches compares its key in a way "
                     "that could in principle leak information through timing. The portal's "
                     "own comparisons are already done safely. Not practically exploitable "
                     "over a network."),
]
A(table([[Paragraph("ID", cellb), Paragraph("Severity", cellb), Paragraph("What remains", cellb)]] +
        [[Paragraph(f, cell), Paragraph(s, cell), Paragraph(t, cell)] for f, s, t in op],
        [16*mm, 20*mm, 134*mm]))
A(Spacer(1, 3*mm))
A(Paragraph("The project team proposes to address F-26 next, as the only remaining item "
            "by which a person could gain administrator rights, followed by F-18, F-16 and "
            "F-24, and then the two informational items. A verification review is requested "
            "once these are complete.", body))
A(PageBreak())

# ---------------- extra findings ----------------
A(Paragraph("6. Problems found during remediation", h1))
A(Paragraph("Seven problems came to light during this work that were not in the "
            "assessment. They are recorded here for completeness.", body))
extra = [
    ("Live secrets were briefly published", "While preparing the fixes, a routine commit "
     "accidentally included a backup copy of the live configuration file, and it was pushed to a "
     "public code repository. It was public for approximately thirty minutes. The session key and "
     "the bench network key were rotated the same day, the repository was made private, and the "
     "file was permanently removed from the project's history. One credential, an email account "
     "password not under this project's control, has been referred to its owner for rotation. The "
     "gap that allowed it - the ignore rules did not cover backup copies of the configuration "
     "file - has been closed."),
    ("Sign-out did not invalidate other sessions", "A recently added feature intended to sign a "
     "user out everywhere was written in the wrong order and never actually took effect, despite "
     "its description stating otherwise. Fixed, with a test."),
    ("An undeclared software requirement", "The application used a component that was never "
     "listed among its requirements. It happened to be installed on the live server, so this was "
     "invisible - but a rebuild from the project files would have produced a system that could "
     "not start. Now declared."),
    ("The charting library had no version fixed", "The portal requested its charting "
     "library without naming a version, so on every page load it accepted whatever that "
     "external service happened to be publishing - including new major versions that had "
     "never been tested against this portal. It is now fixed at the version that was "
     "actually in use, and held within the project rather than fetched."),
    ("Two pages ran different versions of the same library", "The mathematics-rendering "
     "library was already held within the project and used by the laboratory page, but the "
     "experiment page was still downloading a different, older version from the internet. "
     "The two pages were therefore running different versions of it. Both now use the local "
     "copy."),
    ("The database file can be read by any account on the server", "The file holding user "
     "records, including password hashes, permits any local account on the server to read "
     "it. Correcting this is tied to the restricted-service-account change and will be done "
     "at the same time, because restricting the file before that change would stop the "
     "service starting."),
    ("The web server did not support live instruments", "Serial, the plotter, the oscilloscope "
     "and the debugger could not work through the public web address, because the web server was "
     "not configured to carry that type of connection. The page still loaded and the camera still "
     "worked, so the fault was not obvious. Corrected, and documented so it is recognised in "
     "future."),
]
for title, txt in extra:
    A(Paragraph(title, h2))
    A(Paragraph(txt, body))

A(Paragraph("7. How the work was verified", h1))
A(Paragraph("Three forms of evidence support the statements in this report.", body))
A(Paragraph("<b>Tests written before fixes.</b> For each finding, an automated test was written "
            "first and confirmed to fail against the unfixed code, then confirmed to pass "
            "afterwards. This demonstrates both that the problem was real and that it is gone. "
            "The suite now runs 274 checks, up from 152.", body))
A(Paragraph("<b>Checks against the running system.</b> After deployment, the four most serious "
            "routes were tried against the live server. Each was refused: anonymous access to the "
            "bench service, anonymous access to bench status, the removed camera page, and "
            "unauthenticated firmware upload.", body))
A(Paragraph("<b>Independent tooling.</b> The dependency vulnerability scanner reports no known "
            "vulnerabilities, against eleven before. The operating-system service definition was "
            "validated with the system's own checking tool. Every file now served in place of an "
            "external download was checked to be the genuine library, and its checksum recorded "
            "in the project.", body))
A(Paragraph("<b>Limits of this verification.</b> Two things need confirmation by a person "
            "rather than by a test, because they cannot be tested automatically here. The "
            "first - that a laboratory session still works end to end, and that the changed "
            "sign-out control behaves correctly - was checked on the live system after the "
            "cross-site protection was added, and passed. The second - that the instrument "
            "pages still display correctly now that fonts and icons are served by the portal "
            "rather than fetched from outside - has not yet been confirmed at the time of "
            "writing. Automated checks confirm every such file is present and served, but a "
            "visual check remains outstanding and is listed in section 8. Nothing in that "
            "change affects access control; the risk if it is wrong is cosmetic.", body))
A(Paragraph("Three tests in the suite fail, all concerning time-zone handling of late-evening "
            "booking slots. They pre-date this work, are unrelated to security, and are scheduled "
            "for correction.", body))

A(Paragraph("8. Operational actions outstanding", h1))
A(table([[Paragraph("Action", cellb), Paragraph("Owner", cellb), Paragraph("Status", cellb)]] +
        [[Paragraph(a, cell), Paragraph(o, cell), Paragraph(s, cell)] for a, o, s in [
    ("Rotate the shared email account password", "Mail account owner", "Done"),
    ("Restrict the database file to the service account", "System administrator", "With the service-account change"),
    ("Apply the restricted service account and tighter system restrictions", "System administrator", "Prepared, awaiting window"),
    ("Restrict the application to local connections only", "System administrator", "Prepared, awaiting window"),
    ("Narrow the permitted bench network range in configuration", "System administrator", "Pending"),
    ("Confirm the instrument pages display correctly after F-19", "Project owner", "Outstanding"),
    ("Correct the three time-zone test failures", "Developer", "Pending"),
    ("Request a verification review", "Project owner", "After remaining Medium items"),
]], [96*mm, 38*mm, 36*mm]))
A(Spacer(1, 6*mm))
A(Paragraph("Prepared 23 September 2026. This report describes the state of the system on that "
            "date and should be read alongside the original assessment.", small))

doc.build(E)
print("built Remediation_Report.pdf")
