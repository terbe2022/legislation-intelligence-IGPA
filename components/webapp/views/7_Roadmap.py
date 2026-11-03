"""
pages/7_Roadmap.py — Platform Roadmap
"""
import streamlit as st
from auth_utils import require_auth

require_auth()

# ── Accessibility chrome (ReQtest audit 2026-09-14) ──────────────────────────
# Issues 1/2 (main + nav landmarks), 16 (disabled-state contrast),
# 17/19 (keyboard focus on chart and dataframe overlay controls).
# Must run after require_auth() so it is not emitted on the login gate.
from a11y import apply_page_a11y
apply_page_a11y('Roadmap')

st.markdown("""
<style>
/* Bug 26 follow-up: one font app-wide. Inter (a Google Fonts download) was
   used on some pages and Streamlit's built-in Source Sans Pro on others. */
.block-container{
  padding-top:0.8rem !important;padding-left:1.2rem !important;
  padding-right:1.2rem !important;max-width:900px;
}
header[data-testid="stHeader"]{background:transparent !important;height:0 !important;}
[data-testid="stToolbar"]{display:none !important;}
.road-card{
  background:#fff;border:1px solid #E2E8F0;border-radius:12px;
  padding:20px 22px;margin-bottom:12px;
}
.road-title, [data-testid="stMarkdownContainer"] h3.road-title{font-size:16px;font-weight:700;color:#0F172A;margin:0 0 4px;padding:0;line-height:1.35;letter-spacing:normal;}
.road-desc{font-size:13px;color:#475569;line-height:1.65;}
.road-tag{display:inline-block;font-size:11px;font-weight:600;padding:2px 9px;
          border-radius:5px;margin-right:6px;margin-bottom:8px;}
.tag-v1{background:#D1FAE5;color:#065F46;}
.tag-live{background:#DCFCE7;color:#14532D;border:1px solid #86EFAC;}
.tag-next{background:#FEF3C7;color:#92400E;border:1px solid #FCD34D;}
.tag-later{background:#F3F4F6;color:#374151;}
.tag-research{background:#EDE9FE;color:#5B21B6;}
.omit-box{background:#FFFBEB;border:1px solid #FDE68A;border-radius:10px;
          padding:16px 20px;margin-bottom:16px;}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div style="background:linear-gradient(135deg,#0F1F38 0%,#1B4B5A 55%,#2D6A7A 100%);
            border-radius:14px;padding:22px 28px;margin-bottom:16px;">
  <div style="color:rgba(255,255,255,.92);font-size:10px;font-weight:700;
              letter-spacing:.14em;text-transform:uppercase;margin-bottom:6px">
    Platform Roadmap
  </div>
  <h1 style="margin:0;padding:0;color:#fff;font-size:24px;font-weight:800;letter-spacing:-.03em;
              line-height:1.15;margin-bottom:8px">
    What's Coming in v1 &amp; Beyond
  </h1>
  <div style="color:rgba(255,255,255,.92);font-size:13px;line-height:1.6">
    What's live in the platform today, what's next in the build queue, and the
    longer-term research directions. The platform is updated continuously — check
    back often.
  </div>
</div>
""", unsafe_allow_html=True)

# ── Now Live ──────────────────────────────────────────────────────────────────
st.markdown("## Now Live — Your Personalization Engine")
st.caption("Shipped and running today. This is what makes the platform *yours* "
           "rather than a generic bill tracker.")

st.markdown("""
<div class="road-card">
  <span class="road-tag tag-live">Now Live</span>
  <h3 class="road-title">Your Role Profile as a Living Prompt</h3>
  <div class="road-desc">
    In <strong>My Profile</strong> you describe yourself once — your title, your
    functional role, the stakeholders you serve, the areas of legislation you care
    about, and what you actually do day to day. The platform compiles that into a
    standing question and asks it of <strong>every active bill</strong>:
    <em>given who this person is, how does this bill affect them, the people they
    serve, and the work they're responsible for?</em> Every bill the AI investigates
    is scored through the lens of your real role — not a generic "does this touch
    the University" test.
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-live">Now Live</span>
  <h3 class="road-title">Custom Impact Prompts — Up to Five, Your Own Questions</h3>
  <div class="road-desc">
    Beyond your profile, you can write up to <strong>five impact prompts of your
    own</strong> — any question you want asked of the legislation. A specific
    department, a population you're accountable for, a narrow area of law, a program
    you're standing up: "how does this affect graduate student funding," "how does
    this change our procurement obligations," anything you choose. Each prompt is run
    <strong>independently against every active bill</strong>, scored for impact, and
    saved — so the system is continuously answering <em>your</em> questions, not a
    fixed template, every time new legislation moves.
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-live">Now Live</span>
  <h3 class="road-title">My Briefing — Your Searchable Investigation Archive</h3>
  <div class="road-desc">
    Every bill your profile and prompts flagged lands in <strong>My Briefing</strong>,
    the full searchable record of what the AI investigated for you — each with the
    impact score your prompts assigned it. Every bill shows its title, a summary, the
    stage it's at, its floor-vote likelihood, and its last action right on the card,
    with a link to the official text on the Illinois General Assembly site; open any
    bill for the full deep-dive and complete summary. Filter by impact level, by
    <em>which</em> of your prompts flagged it, and by legislative stage; narrow to a
    passage-likelihood band with a live histogram; and sort by likelihood to pass,
    most recent action, impact, or earliest stage. Pin anything straight to My Bills.
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-live">Now Live</span>
  <h3 class="road-title">My Digest — On-Demand Insight Lenses</h3>
  <div class="road-desc">
    Instead of one all-in-one summary, My Digest is a set of <strong>lenses</strong> —
    each a button that generates a focused AI read of a different slice of your data:
    a themed overview of your high-impact bills, the <em>momentum &amp; movers</em>
    (bills gaining steam or already likely to pass), a breakdown of just the bills
    you're tracking, a read filtered to you and your stakeholders, and — for teams —
    the trends your group is converging on and highlights from bills you track
    together. Generate any lens on demand; results stay until you refresh them.
    Wherever a teammate has annotated a bill, that expert read is pulled in and
    <strong>weighted heavily</strong> in the analysis.
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-live">Now Live</span>
  <h3 class="road-title">Teams &amp; Shared Intelligence</h3>
  <div class="road-desc">
    Work as a group. Create teams, invite colleagues by email, and choose to share
    your briefings and tracked bills with your teammates. <strong>Where your team
    overlaps</strong> surfaces the bills the AI independently flagged high-impact for
    you <em>and</em> for teammates — through each person's own prompts and profile,
    not because anyone pinned them — so genuine convergence rises to the top. Because
    your colleagues are analysts who know legislation, their annotations travel with
    the bills they share and feed directly into your digest, so a teammate's read
    shapes how a bill is presented to you.
  </div>
</div>
""", unsafe_allow_html=True)

# ── v1 Launch Features ────────────────────────────────────────────────────────
st.markdown("## In v1 — Active Development")

st.markdown("""
<div class="road-card">
  <span class="road-tag tag-v1">v1 Launch</span>
  <h3 class="road-title">Email Digest Delivery</h3>
  <div class="road-desc">
    Automated weekly (or daily) email digests summarizing your tracked bills and watched
    topics. Powered by Azure Communication Services and Azure OpenAI — a plain-English
    briefing of what's moving, what changed, and what impacts the University of Illinois,
    delivered directly to your inbox. Configurable frequency in My Tracker settings.
    <br><br>
    <em>Status: ACS infrastructure deployed. Azure OpenAI integration in progress.</em>
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-v1">v1 Launch</span>
  <h3 class="road-title">Validation Dashboard — Live Data</h3>
  <div class="road-desc">
    The advance warning framework tracks how many days before a bill reaches
    a floor vote the model first crossed its certainty threshold. Live data populates
    the Validation page as the 104th GA session progresses and bills resolve.
    Tracking started June 17, 2026.
    <br><br>
    <em>Status: In active development. Data collection started June 17, 2026 — meaningful validation results require bills to resolve, which resumes when the legislature reconvenes.</em>
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-v1">v1 Launch</span>
  <h3 class="road-title">Evidence-Backed Impact Ratings</h3>
  <div class="road-desc">
    Every impact rating will carry a verbatim quotation from the bill section that
    justifies it, verified against the source text before it is shown. If the model
    cannot point to specific statutory language, the rating is withheld rather than
    displayed. This closes a known weakness in AI impact analysis: asked whether a
    bill <em>could</em> affect you, a language model will almost always construct a
    plausible chain of consequences. Requiring a citation makes the claim checkable —
    and turns each finding into a direct link to the exact provision.
    <br><br>
    <em>Status: In development. Chunk-level scoring stage built and measured.</em>
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-v1">v1 Launch</span>
  <h3 class="road-title">Section-Level Analysis of Long Bills</h3>
  <div class="road-desc">
    Impact scoring currently reads a condensed summary of each bill. For a 40,000-token
    omnibus or appropriations bill, that summary compresses roughly 200:1 — and a
    provision buried in the middle of a long bill can disappear before your impact
    question is ever asked. The platform will score <strong>every section</strong> of
    every long active bill against your prompts and role profile, so nothing is
    summarized away. Findings will cite the section they came from.
    <br><br>
    <em>Status: Scoring stage built. 2,335 multi-section active bills in the 104th GA;
    validation against expert-labeled examples underway before rollout.</em>
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-v1">v1 Launch</span>
  <h3 class="road-title">Tracked Bills Always Investigated</h3>
  <div class="road-desc">
    A bill you pin is a human judgment no prompt produced. It may matter for reasons
    the AI cannot infer, or you may be watching it for a colleague. Pinned bills will
    always receive a full deep-dive investigation and always appear in your briefing —
    regardless of how the automated impact prompts rated them, and without being
    filtered out by the default impact filter.
    <br><br>
    <em>Status: Design complete. Requires one addition to the nightly export.</em>
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-v1">v1 Launch</span>
  <h3 class="road-title">Longitudinal Policy Impact Tracking</h3>
  <div class="road-desc">
    Track how a policy area (e.g. higher education funding) has evolved across
    multiple GA sessions — what passed, what failed, what's trending up or down.
    With data from the 102nd, 103rd, and 104th General Assemblies now loaded,
    the platform can surface multi-session trends across policy domains, beneficiary
    groups, and ideological alignment to support long-term research and institutional
    memory at IGPA.
    <br><br>
    <em>Status: Data available across 3 sessions. Trends page evolution analysis in progress.</em>
  </div>
</div>
""", unsafe_allow_html=True)

# ── Coming Next ───────────────────────────────────────────────────────────────
st.markdown("## Coming Next — In Design")
st.caption("Scoped and close, not yet built. Where the platform goes after the "
           "current release — shaped directly by the first onboarding sessions.")

st.markdown("""
<div class="road-card">
  <span class="road-tag tag-next">Coming Next</span>
  <h3 class="road-title">Search &amp; Pin — One Place to Find Everything</h3>
  <div class="road-desc">
    Today, finding bills is split across My Bills (which also holds a find-a-bill
    search) and My Topics (your categories). We're merging them. <strong>Search &amp;
    Pin</strong> becomes the first tab you land on — a single discovery surface where
    you search across everything: your interest categories, your impact areas, any
    field of any bill, and — new — bills your <strong>team, or a specific teammate,
    has already pinned</strong>. Find what matters, click <strong>Pin</strong>, and
    it's saved.
    <br><br>
    <strong>My Bills</strong> then becomes exactly what its name says: your personal
    board of the individual bills <em>you</em> chose to track, with your annotations —
    nothing else. Discovery lives in Search &amp; Pin; your curated pin board lives in
    My Bills. This is a large build and the clear next step after the current release.
    <br><br>
    <em>Status: Designed. Sequenced as the next major structural change.</em>
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-next">Coming Next</span>
  <h3 class="road-title">Bill Dossiers — Email Intake &amp; Team Deliberation</h3>
  <div class="road-desc">
    The most valuable legislative analysis this organization produces already exists —
    it just lives in email. A bill is introduced, a thread starts, and people from budget,
    policy, academic affairs, and government relations each add the piece of it they can
    see. By the end the group has reasoned its way to a real position. Then the thread is
    archived, and that reasoning is effectively gone.
    <br><br>
    Every bill gets a <strong>dossier</strong>: one place holding the correspondence,
    documents, transcripts, and notes your team has generated about it. The key design
    decision is that <strong>you don't have to change where you work</strong>. Rather than
    asking anyone to re-upload a conversation they already had, the platform gives you an
    <strong>intake address you simply CC</strong>. Forward or copy a thread and it routes
    itself to the right bill automatically, using the same semantic matching that already
    powers search — and because email carries its own sender information, every
    contribution arrives correctly attributed to the person who wrote it. Drag-and-drop
    stays available for meeting transcripts, PDFs, and slides.
    <br><br>
    Each dossier accumulates toward a <strong>position record</strong>: support, oppose,
    monitor, or no action — with the reasoning behind it, the evidence, the
    stakeholders, and who contributed what. That record is the durable artifact, and it's
    what makes the institutional memory below possible.
    <br><br>
    <em>Status: Scoped. The CC intake path is the priority — it is the cheapest
    capture mechanism available and the only one that asks for no change in habit.</em>
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-next">Coming Next</span>
  <h3 class="road-title">Committee Hearing Alerts — Before, Not After</h3>
  <div class="road-desc">
    Learning that a bill was discussed in committee yesterday is history. Learning it is on
    tomorrow's agenda is leverage — time to prepare, brief a stakeholder, or suggest
    language. Today that early warning depends on phone calls to the clerk's office and on a
    colleague remembering to pass word along.
    <br><br>
    The Illinois General Assembly already publishes committee postings, and the clerk's
    office will send them directly to any address that asks to be added. Pointed at the same
    intake mailbox that feeds bill dossiers, those postings become a <strong>live schedule
    feed</strong> — parsed on arrival, matched against the bills you track, and pushed
    to you as an alert. Hearings are added to the calendar with less than a day's notice,
    which is precisely why polling a website is the wrong mechanism and a subscription is
    the right one.
    <br><br>
    <em>Status: Scoped. Depends on establishing a posting subscription with the clerk's
    office; low build cost once the feed is flowing.</em>
  </div>
</div>
""", unsafe_allow_html=True)

# ── Longer term ───────────────────────────────────────────────────────────────
st.markdown("## Longer-Term Vision")

st.markdown("""
<div class="road-card">
  <span class="road-tag tag-later">Future</span>
  <h3 class="road-title">Cross-Session Memory — When a Bill Comes Back</h3>
  <div class="road-desc">
    Legislation repeats. The same idea returns session after session, renumbered and lightly
    rewritten, and the team analyzes it from scratch every time. A concrete example from this
    session: the bill now tracked as <strong>HB1581 / SB0013</strong> appeared in the previous
    session as <strong>HB5907 / SB3965</strong>.
    <br><br>
    Once dossiers and position records exist, they become an <strong>analysis archive</strong>.
    When a bill is introduced, the platform matches it against everything the organization has
    already concluded and tells you so: <em>you analyzed substantially this legislation last
    session, here is the position you took, here is who was involved and what each of them
    argued, and here is what has changed since.</em>
    <br><br>
    This is the capability a vendor platform structurally cannot offer, because it isn't built
    from legislative data — it's built from your own history. Every session the platform
    is used, it gets harder to replace.
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-later">Future</span>
  <h3 class="road-title">Three-Stage Passage Prediction</h3>
  <div class="road-desc">
    Today's prediction model tracks a bill's floor-vote likelihood <em>through time</em>
    once it's active — that's the middle stage. The vision is to bracket it with two
    more, so the platform can forecast a bill across its entire life:
    <br><br>
    <strong>Stage 1 — Before introduction.</strong> Predict, from external signals
    ahead of a bill ever being filed, what's likely coming and how it may fare — using
    data outside the legislature's own record.
    <br>
    <strong>Stage 2 — Active bill (live today).</strong> The current model: tracking
    passage likelihood, momentum, and the probability trail as a bill moves through the
    session.
    <br>
    <strong>Stage 3 — After committee.</strong> Once a bill reaches committee, predict
    its odds of passing from what's newly knowable — the committee it landed in, its
    composition, and who is positioned to vote on it — the sharpest signal of all,
    because it's the moment a bill's fate starts to concentrate.
    <br><br>
    Together these turn a single live estimate into a full pre-, during-, and
    post-committee forecast.
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-later">Future</span>
  <h3 class="road-title">News Context Integration</h3>
  <div class="road-desc">
    Incorporate news coverage into the AI's understanding of tracked bills.
    Bill text alone can be misleading — a funding bill may read as an increase
    while news coverage reveals the university is actively opposing it. The
    plan: for bills tracked by users, aggregate across all teams down to the
    unique set of tracked bills (one scrape serves everyone tracking the same
    bill), pull recent news via API or AI-powered search, and inject that
    context into impact assessments and email digests. When leadership
    publicly argues against a bill, the platform's analysis should know it
    and reflect the institution's actual position — reading between the
    lines the way an informed analyst would.
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-later">Future</span>
  <h3 class="road-title">Multi-State Expansion</h3>
  <div class="road-desc">
    The pipeline is designed for multi-state operation. Expanding to Indiana, Wisconsin,
    and Missouri would allow IGPA researchers to track regional legislative trends and
    compare policy approaches across neighboring states.
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-later">Future</span>
  <h3 class="road-title">Campaign Finance Integration</h3>
  <div class="road-desc">
    Joining legislative data with Illinois State Board of Elections campaign finance
    records to surface connections between donor activity and bill sponsorship patterns.
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-later">Future</span>
  <h3 class="road-title">Floor Vote Summaries</h3>
  <div class="road-desc">
    When bills reach a floor vote, auto-generate a plain-English summary of how
    representatives voted, party-line vs bipartisan breakdown, and what the vote
    outcome means for University of Illinois priorities.
  </div>
</div>
""", unsafe_allow_html=True)

# ── Research directions ───────────────────────────────────────────────────────
st.markdown("## Research Directions")

st.markdown("""
<div class="road-card">
  <span class="road-tag tag-research">Research</span>
  <h3 class="road-title">The Deliberation Agent — Learning How Your Team Reasons</h3>
  <div class="road-desc">
    Two other directions on this roadmap teach the platform about <em>the institution</em>: the
    policy-aware agent grounds each bill in the University's governance corpus, and the
    feedback loop learns from the ratings you mark right or wrong. This is a third and sharper
    axis — grounding the platform in <strong>how the people here actually reason</strong>.
    <br><br>
    Every dossier is a record of expert judgment: what a budget analyst noticed that a policy
    director didn't, which provision someone flagged as a hidden cost, where two colleagues
    read the same language differently and why. With enough of that captured and correctly
    attributed, the platform can begin to model perspective — not only what the
    organization decided, but how each person arrived there. The goal is an analyst that can
    say <em>this matters to you specifically, and here is why it matters to you and not to
    your colleague.</em>
    <br><br>
    Where the team disagrees, the agent's job is not to adjudicate. It is to lay out each
    position clearly, explain the reasoning behind it, and surface what would have to be true
    for each to be right. Where the record has a hole — a provision nobody with the
    relevant expertise has weighed in on — it should notice, and ask the right person.
    <br><br>
    Two constraints shape the design from day one. <strong>Attribution is structural, not
    cosmetic</strong>: every extracted claim binds to a person, a timestamp, and the exact
    sentence it came from, or the reasoning can never be reconstructed later. And
    <strong>you own your own perspective</strong>: any reading the AI produces of what you
    said is a draft you can see and correct before anyone else sees it. Those corrections
    happen to be the cleanest training signal the platform can collect.
    <br><br>
    <em>Status: Research, and entirely gated on the deliberation corpus. This agent is what
    emerges once the dossiers hold data — not something that can be built ahead of them.</em>
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-research">Research</span>
  <h3 class="road-title">Off-Text Amendment Detection</h3>
  <div class="road-desc">
    A bill's meaning can change without its text changing. An amendment is described aloud in
    committee, or recorded only as a line in the actions history, and a straight comparison of
    one printed version against the next shows nothing at all. Anyone relying on text diffs
    alone quietly misses it — a gap that exists in every tracking platform, not just
    this one.
    <br><br>
    A promising path is already sitting inside the existing model. The passage-prediction work
    learned which legislative action language carries signal — <em>re-referred to
    Rules</em>, <em>held on calendar</em>, <em>tabled</em> — and committee members use
    that same vocabulary when they speak. The feature importances already learned for
    predicting floor votes may transfer directly to committee discussion, letting the platform
    flag when what was <em>said</em> implies a change the text does not yet reflect.
    <br><br>
    <em>Status: Research. Full capability requires hearing transcripts as an input; the
    actions-history signal can be developed independently in the meantime.</em>
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-research">Research</span>
  <h3 class="road-title">Grounding Impact in University Policy</h3>
  <div class="road-desc">
    Today the platform asks "how does this bill impact the University?" — a question
    with no wrong answer, because almost all legislation affects almost everyone
    indirectly. The stronger question is a comparison: <em>what does this bill change
    about an obligation the University already has?</em> By ingesting the University of
    Illinois System policy library alongside legislative text, an impact finding becomes
    a concrete delta — "this section changes an annual reporting requirement to
    quarterly, amending Policy §4" — which a human can verify against both documents.
    This is something no commercial legislative-tracking vendor can build: it requires
    the institution's own governance corpus.
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-research">Research</span>
  <h3 class="road-title">Policy-Aware AI Analyst Agent &amp; Live Knowledge Base</h3>
  <div class="road-desc">
    The platform flags bills two ways today: by <em>who you are</em> (your role profile
    and impact prompts) and by <em>broad University of Illinois impact</em>. This adds a
    third, sharper axis — <em>resemblance to the policies, laws, and regulations the
    University already has to follow.</em> Instead of asking whether a bill matters in the
    abstract, the agent asks: does this bill <strong>amend an obligation we already
    carry</strong> (a critical, must-know change), open a <strong>brand-new area</strong>
    we have no policy for yet, or simply restate something already on the books?
    <br><br>
    Mechanically, this is a retrieval-augmented <strong>agent grounded in a knowledge base
    built from the University's own governance corpus</strong> — the policy library
    published across University and campus sites. Rather than following a fixed script, the
    agent decides, per bill, what to pull: the full bill text, its amendment history,
    related statute, and the specific policy sections it most resembles — then reports a
    concrete delta ("HB #### moves an annual reporting requirement to quarterly, amending
    Policy §X"), verifiable against both documents. This is the agentic evolution of the
    current two-stage pipeline: model-driven tool use and routing, pointed at institutional
    knowledge.
    <br><br>
    The hard part is <strong>keeping the corpus current.</strong> Policies change on their
    own cadence — monthly, quarterly, annually — and an obligation comparison is only as
    good as the version it's compared against. So the system needs a live, machine-readable
    feed of policy documents: change detection that notices when a policy is revised,
    re-ingestion of what changed, and a re-scan of active bills against the updated corpus.
    That in turn requires working with <strong>IGPA and the University of Illinois System</strong>
    to maintain a continuously updated digital record of the policy library — the
    institutional dependency that makes this both the most defensible and the most
    ambitious direction on this roadmap.
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-research">Research</span>
  <h3 class="road-title">Obligation Extraction (Deontic Classification)</h3>
  <div class="road-desc">
    Established work in legal NLP classifies statutory language by what it does —
    obligation, permission, prohibition, recommendation — rather than by how important
    it seems. The Institutional Grammar framework (Crawford &amp; Ostrom) decomposes a
    provision into who is bound, what they must do, and under what condition. Adopting
    this vocabulary would let the platform report that a bill "imposes a new reporting
    obligation on public universities," a claim that is either true or false, in place
    of a rating on a four-point scale.
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-research">Research</span>
  <h3 class="road-title">Measured Precision &amp; Recall</h3>
  <div class="road-desc">
    Legal e-discovery established decades ago that an automated review system is only
    defensible when its accuracy is measured against a human-labeled sample. The
    platform will maintain an expert-coded evaluation set of bill sections, allowing us
    to state plainly how many genuinely relevant provisions the model finds, and how
    many of its flags are noise — and to detect when a model or prompt change makes
    either number worse.
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-research">Research</span>
  <h3 class="road-title">Legislative Knowledge Graph</h3>
  <div class="road-desc">
    Bills, sponsors, committees, actions, and the statutes they amend form a network.
    Some questions are awkward as database queries but natural as graph traversals —
    <em>which bills amend a statute that a bill I already track also amends, sponsored
    by a member of the committee where my bill is currently stuck?</em> The evaluation
    gate is deliberate: we will build the graph when we can name three such questions
    that stakeholders actually want answered, starting with the amended-statute
    relationships already extracted from every bill.
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-research">Research</span>
  <h3 class="road-title">Campaign Finance &amp; Sponsorship Networks</h3>
  <div class="road-desc">
    Donor activity and sponsorship patterns are plausible features for predicting
    whether a bill reaches a floor vote — a different question from whether it impacts
    the University. Any such feature raises a governance question for a nonpartisan
    public-policy institute before it raises an engineering one, and would be developed
    in consultation with IGPA leadership rather than shipped as a technical capability.
  </div>
</div>

<div class="road-card">
  <span class="road-tag tag-research">Research</span>
  <h3 class="road-title">A Model That Learns Your Institution</h3>
  <div class="road-desc">
    Every day the platform is used, it collects signal about what <em>your</em>
    organization actually finds impactful: the profiles people write, the bills they
    annotate, and — most directly — the flags they mark right or wrong when the AI
    scores a bill. Each of those is a labeled judgment from someone who knows the
    work. The direction is to feed that judgment back into the model, reinforcing what
    it got right and correcting what it got wrong, so it steadily answers the
    University's questions better than any general-purpose system could.
    <br><br>
    Over time this compounds. The more the platform is used, the more tailored,
    specific, and fluent in the institution's priorities it becomes — turning the
    current pipeline into a true <strong>AI analyst that has learned this
    organization</strong>, not a generic tool pointed at it. The same feedback that
    makes the analyst smarter also measures it: the up/down signals double as the
    evaluation set behind <em>Measured Precision &amp; Recall</em>, so improvement is
    something we can demonstrate, not just assert. In short — the data the platform
    gathers today is what makes it better tomorrow, for this institution specifically.
  </div>
</div>
""", unsafe_allow_html=True)

st.markdown(__import__("os").environ.get('LEG_COMPONENTS_WEBAPP_VIEWS_7_ROADMAP_PY_VALUE_L604', ""), unsafe_allow_html=True)
