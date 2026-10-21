import logoUrl from "./assets/persei-logo.png";
import { SKY_FAR, SKY_MAIN, SKY_NEAR } from "./constellation";
import shotPipeline from "./assets/shots/models2.webp";
import shotAnalytics from "./assets/shots/analytics2.webp";
import shotLedger from "./assets/shots/analytics3.webp";
import shotReviewer from "./assets/shots/models3.webp";
import shotSupport from "./assets/shots/models4.webp";
import shotUsers from "./assets/shots/users2.webp";
import shotPlanning from "./assets/shots/dashboard2.webp";
import "./LandingPage.css";

const REPO = "https://github.com/DJG3DK/persei";

// Where the newsletter form posts. Same origin as this page, so the form is a
// plain HTML POST with no fetch, no CORS and no JavaScript -- which is what
// keeps this page static. nginx routes it to site/server.
const SUBSCRIBE_URL = "/newsletter/subscribe";

// Who the newsletter and this page come from.
const OWNER_EMAIL = "danny@perseiagent.ai";

function GitHubMark() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" width="15" height="15" fill="currentColor">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.42 7.42 0 0 1 2-.27c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z" />
    </svg>
  );
}

/* An arrow into a tray. Decorative: the button says what it does, and a
   screen reader should hear that sentence once, not twice. */
function DownloadMark() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" width="15" height="15" fill="none"
         stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 1.5v8.5" />
      <path d="M4.5 7 8 10.5 11.5 7" />
      <path d="M2 12.5v1a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1v-1" />
    </svg>
  );
}

/* The five stages a task moves through, as the outer graph actually runs them.
   "Review gate" is marked because it is the one that can send work backwards. */
const PIPELINE = [
  { label: "Plan", note: "write_todos" },
  { label: "Build", note: "sandboxed" },
  { label: "Verify", note: "real test suite" },
  { label: "Review gate", note: "2nd model", gate: true },
  { label: "Ship", note: "on green CI" },
];

// Same host as this page; site/server answers with the latest full
// release's installer, so the link never names a version that goes stale.
// A short form first (name, email, an opt-in for updates), served by
// site/server; it starts the download and shows Linux the one-line installer.
const WINDOWS_DOWNLOAD = "/get/windows";
const LINUX_DOWNLOAD = "/get/linux";
const LINUX_DEB_DOWNLOAD = "/get/linux-deb";

const STAGES = [
  {
    kicker: "work",
    title: "It builds in a box that can only see one repo",
    body: `The agent works inside a throwaway Docker container with just the one repository
      mounted. A bad shell command can't wander into your other projects or your secrets. When
      it needs help it hands off to two helpers: one that can only read and investigate, and one
      that writes tests and has to actually run them before it's allowed to say it's done.`,
  },
  {
    kicker: "verify_and_ship",
    title: "Saying “done” doesn't count",
    body: `I don't trust the agent's word, so the gate re-runs your project's real typecheck,
      lint and tests itself, the same commands you'd run. Only a pass with an actual diff becomes a
      commit, on the task's own branch. If the agent left items on its todo list, the commit is held
      and the task goes back to finish them.`,
  },
  {
    kicker: "review",
    title: "A second model reviews it, then you do",
    body: `A different model reviews the branch against the exact point it forked from, runs
      the checks again in its own sandbox, and says READY or NEEDS_FIXES with findings. Then it
      waits for your look. What merges is the exact commit that was reviewed, and main only
      moves when GitHub Actions is green on it too.`,
  },
];

// Memory and reflection: what makes the tenth task on a repo better than the first.
const LEARNING = [
  {
    kicker: "reflect",
    title: "It stops and thinks after a miss",
    body: `When the tests fail or the reviewer sends it back, the agent doesn't just try again.
      It writes down what failed, why, and the one thing to do differently, and starts the next
      attempt from that. It also sees what it already tried, so it doesn't repeat itself.`,
  },
  {
    kicker: "memory",
    title: "Each project has its own memory",
    body: `Every finished task leaves a short record: what was asked, what happened, what it cost,
      what the reviewer said. Once a day the agent reads the recent ones and keeps what held up,
      like how this repo runs its tests or which mistake keeps coming back, and drops the noise.`,
  },
  {
    kicker: "recall",
    title: "“Have we seen this before?”",
    body: `Months of tasks, transcripts and errors are searchable. When an error looks familiar,
      the agent can find the time it already solved it instead of starting from zero.`,
  },
  {
    kicker: "freshness",
    title: "Memory that knows when it's stale",
    body: `A remembered fact points at the files it came from. When those files change, the fact
      gets flagged as possibly out of date, so old knowledge doesn't quietly steer new work.`,
  },
];

const FEATURES = [
  {
    img: shotLedger,
    alt: "Analytics — per-role model usage with call counts, tokens, latency, cost and cache rate",
    title: "See where the money actually went",
    body: `Every role's real usage, straight from the router's own per-call ledger: calls,
      tokens, latency, cost, and how much came from cache. If a role is burning money, you see it
      here instead of finding out from the bill.`,
  },
  {
    img: shotReviewer,
    alt: "The commit reviewer's role, showing how many probed models meet its requirements",
    title: "Models get tested before you pin them",
    body: `The reviewer needs strict tool calling and a forced response shape, and most models
      can't do both. Each role only lists the models that actually passed its probes, so you find
      out on this page, not four minutes into a task.`,
  },
  {
    img: shotSupport,
    alt: "Support roles — classifier, summarizer, vision and cartographer, each with capability badges",
    title: "Cheap jobs get cheap models",
    body: `Classifying a task or summarising a log doesn't need the model that writes your code,
      and paying coder prices for it is just waste. Every support role is pinned on its own and
      says what it actually needs.`,
  },
  {
    img: shotUsers,
    alt: "Users — per-project access control and adding a user",
    title: "People see the projects they need",
    body: `An account is scoped to the projects you give it, and everything follows that: tasks,
      planning, analytics. You see everything; someone you add for one repo can't start work on
      another.`,
  },
];

// What landed in 0.9. Short on purpose: the changelog has the long version.
const NEW_IN_09 = [
  {
    title: "It learns from a miss",
    body: "After a failed attempt the agent writes down what went wrong and what to try next, then starts there. Fewer rounds of the same fix.",
  },
  {
    title: "A real Windows app",
    body: "One installer. It sets up WSL and Docker if you don't have them, asks for your keys in a window, pulls the release and signs you in.",
  },
  {
    title: "It keeps itself updated",
    body: "The app and the stack update on their own, but only when the agent is idle, and never by grabbing your window mid-task.",
  },
  {
    title: "Signed releases",
    body: "Every release builds from a tag on a green main, pushes its images once by digest, and signs the list the app pulls from. I approve each one before it ships.",
  },
  {
    title: "Catching problems before they become problems",
    body: "The review gate got tougher. If a review didn't actually check the code, it doesn't pass. A task waiting its turn behind other reviews keeps its place instead of timing out, and your approval carries through a rebase that changed nothing.",
  },
  {
    title: "Less busywork from GitHub",
    body: "Dependabot alerts on the same package become one task, not eleven, and a task whose alert another fix already closed just says so and stops.",
  },
  {
    title: "A “Needs you” list",
    body: "Anything escalated or waiting on your merge approval sits in its own group at the top of the sidebar until it's done.",
  },
];

const CONTROLS = [
  {
    title: "A budget cap on every task",
    body: "Checked after every model call, on the main agent and every helper, so a loop that goes sideways costs a number you picked, not whatever it wants.",
  },
  {
    title: "The agent can't push to git",
    body: "Its shell has no SSH key, no git credentials and no token, so a push from there has nothing to log in with. Only the gate pushes, after review, using a fine-grained GitHub token you create for just the repositories it should touch, handed to that one push and never to the agent.",
  },
  {
    title: "Work can come straight from GitHub",
    body: "The inbox picks up Dependabot PRs, security and code-scanning alerts, review comments and failing checks. Propose sends you an approve link; Auto just starts it. Either way it goes through the review gate and waits for your merge approval.",
  },
  {
    title: "It asks instead of guessing",
    body: "When it hits something gated, or it's genuinely unsure, the question shows up in the task stream, and your answer goes right back into the paused run.",
  },
  {
    title: "The server decides what runs",
    body: "Projects can only come from folders you allowed, checked after symlinks resolve. The commands the gate runs are the ones the server proposed, so nobody can slip a new one in through a request.",
  },
  {
    title: "Nothing is a dead end",
    body: "Escalated, stopped, out of budget or cut off by a restart, the whole state is saved, so resume picks up the same run instead of starting over.",
  },
];

/* The constellation from the logo, in 3D: the stars and their lines on one
   plane (so a line always meets its star), two starfields set far behind it,
   and the whole scene swaying slowly in perspective -- the depth shows as the
   layers slide against each other. CSS only, because this page ships no
   JavaScript (scripts/render.mjs). Decorative, so hidden from screen readers.
   The SVG is generated by scripts/brand/build_hero.py. */
function Constellation() {
  return (
    <div className="lp-sky" aria-hidden="true">
      <div className="lp-sky-scene">
        <div className="lp-sky-plane" dangerouslySetInnerHTML={{ __html: SKY_FAR }} />
        <div className="lp-sky-plane" dangerouslySetInnerHTML={{ __html: SKY_NEAR }} />
        <div className="lp-sky-plane" dangerouslySetInnerHTML={{ __html: SKY_MAIN }} />
      </div>
    </div>
  );
}

export function LandingPage() {
  return (
    <div className="landing">
      <header className="lp-nav">
        <div className="lp-nav-inner">
          {/* The lockup already reads "Persei", so no wordmark beside it. */}
          <a className="lp-brand" href="#top" aria-label="Persei, back to top">
            <img src={logoUrl} alt="Persei" />
          </a>
          <nav className="lp-nav-links">
            <a href="#how">How it works</a>
            <a href="#memory">Memory</a>
            <a href="#new">What's new</a>
            <a href="#controls">Controls</a>
            <a href="#install">Install</a>
          </nav>
          <div className="lp-nav-actions">
            <a className="lp-ghost" href={REPO} target="_blank" rel="noopener noreferrer">
              <GitHubMark />
              <span>GitHub</span>
            </a>
            <a className="lp-signin" href="#newsletter">
              Get updates
            </a>
          </div>
        </div>
      </header>

      <main id="top">
        <section className="lp-hero">
          <div className="lp-hero-top">
            <div className="lp-hero-copy">
              <p className="lp-eyebrow">Self-hosted &middot; Windows app &middot; LangGraph &middot; any model, via OpenRouter</p>
              <h1>
                An autonomous coding agent{" "}
                <br />
                that has to prove its work.
              </h1>
              <p className="lp-lede">
                Persei was created out of a need for a truly autonomous AI developer you can trust
                with your repositories. Simply provide a goal in plain English, and the agent
                independently plans the solution, writes the code, and executes your project&rsquo;s
                local test suite. To ensure absolute production safety, a secondary model reviews the
                code before it ever reaches the main branch. Persei runs locally on your own machine,
                giving you full control to plug in any model via OpenRouter.
              </p>
              <div className="lp-cta">
                <a className="lp-btn lp-btn-primary" href={WINDOWS_DOWNLOAD}>
                  <DownloadMark />
                  Download for Windows
                </a>
                <a className="lp-btn lp-btn-primary" href={LINUX_DOWNLOAD}>
                  <DownloadMark />
                  Download for Linux
                </a>
                <a className="lp-btn lp-btn-quiet" href={REPO} target="_blank" rel="noopener noreferrer">
                  <GitHubMark />
                  View the source
                </a>
              </div>
            </div>
            <Constellation />
          </div>

          <ol className="lp-pipeline" aria-label="Task pipeline">
            {PIPELINE.map((s, i) => (
              <li key={s.label} className={s.gate ? "is-gate" : undefined}>
                <span className="lp-pipe-index">{i + 1}</span>
                <span className="lp-pipe-label">{s.label}</span>
                <span className="lp-pipe-note">{s.note}</span>
              </li>
            ))}
          </ol>

          <figure className="lp-shot lp-shot-hero">
            <div className="lp-chrome" aria-hidden="true">
              <span /> <span /> <span />
            </div>
            <img src={shotPipeline} alt="Model configuration — the build pipeline roles, each with its own pinned model and live pricing" />
          </figure>
        </section>

        <section id="how" className="lp-section">
          <h2 className="lp-h2">How a task runs</h2>
          <p className="lp-sub">
            It&rsquo;s a loop. The agent does the work, the gate decides whether that work earns
            a merge, and the gate is allowed to send it back as many times as it takes.
          </p>
          <div className="lp-stages">
            {STAGES.map((s) => (
              <article key={s.kicker} className="lp-stage">
                <code className="lp-kicker">{s.kicker}</code>
                <h3>{s.title}</h3>
                <p>{s.body}</p>
              </article>
            ))}
          </div>
          <p className="lp-pullquote">
            &ldquo;The agent saying <em>done</em> doesn&rsquo;t mean it&rsquo;s done.&rdquo;
          </p>
        </section>

        <section id="memory" className="lp-section">
          <h2 className="lp-h2">It learns from its own work</h2>
          <p className="lp-sub">
            Most agents start every task from scratch. This one keeps notes, on each project and
            on its own mistakes, so the tenth task on a repo goes better than the first.
          </p>
          <div className="lp-stages lp-stages-4">
            {LEARNING.map((s) => (
              <article key={s.kicker} className="lp-stage">
                <code className="lp-kicker">{s.kicker}</code>
                <h3>{s.title}</h3>
                <p>{s.body}</p>
              </article>
            ))}
          </div>
          <p className="lp-pullquote">
            &ldquo;A lesson is only trusted once the task that learned it ships.&rdquo;
          </p>
        </section>

        <section id="console" className="lp-section">
          <h2 className="lp-h2">The console</h2>
          <p className="lp-sub">
            Everything happens in one dashboard. You watch tasks and planning live, and if you open
            it halfway through a task you see what already happened instead of a blank page. It
            installs on your phone too.
          </p>
          <figure className="lp-lead">
            <div className="lp-shot">
              <div className="lp-chrome" aria-hidden="true">
                <span /> <span /> <span />
              </div>
              <img
                src={shotAnalytics}
                alt="The dashboard — spend, outcomes and per-repo cost, with planning sessions and tasks grouped by category in the sidebar"
              />
            </div>
            <figcaption>
              Planning sessions and tasks share one sidebar, grouped by kind, filterable by repo,
              with anything waiting on you pinned at the top. Spend, outcomes and fix cycles are
              up front, and the review gate&rsquo;s cost is counted separately from the
              agent&rsquo;s. Your remaining credit turns red under 15%, so you see it coming.
            </figcaption>
          </figure>
          <div className="lp-features">
            {FEATURES.map((f, i) => (
              <article key={f.title} className={`lp-feature${i % 2 ? " is-flipped" : ""}`}>
                <figure className="lp-shot">
                  <div className="lp-chrome" aria-hidden="true">
                    <span /> <span /> <span />
                  </div>
                  <img src={f.img} alt={f.alt} loading="lazy" decoding="async" />
                </figure>
                <div className="lp-feature-copy">
                  <h3>{f.title}</h3>
                  <p>{f.body}</p>
                </div>
              </article>
            ))}
          </div>
        </section>

        <section id="new" className="lp-section">
          <h2 className="lp-h2">New in 0.9</h2>
          <p className="lp-sub">
            The biggest release so far. Most of it came from actually running it every day on my
            own projects and fixing whatever got in my way.
          </p>
          <div className="lp-controls">
            {NEW_IN_09.map((c) => (
              <article key={c.title} className="lp-control">
                <h3>{c.title}</h3>
                <p>{c.body}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="lp-section lp-planning">
          <div className="lp-planning-copy">
            <h2 className="lp-h2">Planning Chat</h2>
            <p className="lp-sub">
              Before something gets built, you can talk it through. Planning is its own agent
              with web search, a real browser and read-only access to your projects, but no way to
              change anything. When the plan is right, one click hands it to the build pipeline,
              and what planning cost is carried onto the task.
            </p>
            <p className="lp-sub">
              It picks the model per turn: an everyday one, a stronger one when the question gets
              hard, and a frontend seat for UI work so the plan is written by the model that will
              build it. Once a session steps up, a short follow-up won&rsquo;t quietly drop it back
              down.
            </p>
            <figure className="lp-shot lp-planning-shot">
              <div className="lp-chrome" aria-hidden="true">
                <span /> <span /> <span />
              </div>
              <img
                src={shotPlanning}
                alt="Starting a planning session — choosing the repository and the model route"
                loading="lazy"
                decoding="async"
              />
            </figure>
          </div>
          <ul className="lp-facts">
            <li>
              <strong>Memory that builds up</strong>
              <span>Finished tasks turn into per-project memory, and past work is found by meaning as well as keywords. A cartographer keeps a map of each codebase current.</span>
            </li>
            <li>
              <strong>A dollar cap per turn</strong>
              <span>Planning used to run uncapped. Then it spent $7 on a single 157-call turn. It has a cap now.</span>
            </li>
            <li>
              <strong>Map first, then look around</strong>
              <span>It reads the codebase map before it starts listing folders. One read instead of a dozen.</span>
            </li>
          </ul>
        </section>

        <section id="controls" className="lp-section">
          <h2 className="lp-h2">What the model can&rsquo;t get around</h2>
          <p className="lp-sub">
            This thing writes and runs code against your repos. The guardrails aren&rsquo;t an
            afterthought, they&rsquo;re most of the work.
          </p>
          <div className="lp-controls">
            {CONTROLS.map((c) => (
              <article key={c.title} className="lp-control">
                <h3>{c.title}</h3>
                <p>{c.body}</p>
              </article>
            ))}
          </div>
        </section>

        <section id="install" className="lp-section lp-close">
          <h2 className="lp-h2">Run it yourself</h2>
          <p className="lp-sub">
            Windows and Linux both get an app. On Linux you can also run it straight on the host
            or as a Docker bundle. Every path ends at the same dashboard.
          </p>
          <div className="lp-installs">
            <article className="lp-install">
              <code className="lp-kicker">windows 10 / 11</code>
              <h3>The Windows app</h3>
              <p>
                Download the installer and run it. If you don&rsquo;t have WSL 2 and Docker
                Desktop, it installs them for you (Windows will ask permission, and may want a
                restart). Then it asks for your OpenRouter key and where your projects live, pulls
                the release, and opens the dashboard. From then on it updates itself.
              </p>
              <ol className="lp-steps">
                <li>Download the installer and run it</li>
                <li>Let it set up Docker if it asks</li>
                <li>Enter your OpenRouter key and projects folder</li>
                <li>Pick your password and sign in</li>
              </ol>
              <div className="lp-cta lp-cta-left">
                <a className="lp-btn lp-btn-primary" href={WINDOWS_DOWNLOAD}>
                  <DownloadMark />
                  Download for Windows
                </a>
              </div>
              <p className="lp-reqs">Windows 10 or 11, 64-bit &middot; an OpenRouter API key</p>
            </article>
            <article className="lp-install">
              <code className="lp-kicker">appimage / .deb</code>
              <h3>The Linux app</h3>
              <p>
                Same app, same setup. The first time you run it, it installs itself into your app
                menu. If Docker isn&rsquo;t there it installs it (on Arch-based distros like CachyOS
                too), asking for your password once, and then just carries on: no logging out. Your
                projects stay owned by you, not root. It updates itself from then on.
              </p>
              <ol className="lp-steps">
                <li>Download the AppImage and run it once (the download page also has a one-line installer)</li>
                <li>Let it set up Docker if it asks</li>
                <li>Enter your OpenRouter key and projects folder</li>
                <li>Pick your password and sign in, and find it in your app menu after that</li>
              </ol>
              <div className="lp-cta lp-cta-left">
                <a className="lp-btn lp-btn-primary" href={LINUX_DOWNLOAD}>
                  <DownloadMark />
                  Download the AppImage
                </a>
                <a className="lp-btn lp-btn-quiet" href={LINUX_DEB_DOWNLOAD}>
                  .deb for Debian / Ubuntu
                </a>
              </div>
              <p className="lp-reqs">64-bit Linux with a desktop &middot; an OpenRouter API key</p>
            </article>
            <article className="lp-install">
              <code className="lp-kicker">./install.sh</code>
              <h3>Linux, on the host</h3>
              <p>
                This is how I run it. The installer takes a fresh clone to a running agent:
                prerequisites, secrets, database, sandbox image and dashboard. It asks three
                questions and figures out the rest. <code>--dry-run</code> shows everything it
                would do without doing it.
              </p>
              <pre><code>{`git clone ${REPO}.git
cd persei && ./install.sh`}</code></pre>
              <p className="lp-reqs">
                Linux &middot; Python 3.12+ &middot; Node 24+ &middot; Docker &middot;
                PostgreSQL 14+ &middot; an OpenRouter API key
              </p>
            </article>
            <article className="lp-install">
              <code className="lp-kicker">docker compose</code>
              <h3>Linux, as a bundle</h3>
              <p>
                Agent, database, router and the review gate as one stack, if you&rsquo;d rather
                only install Docker. Put your OpenRouter key and projects folder in <code>.env</code>,
                start it, and open <code>localhost:8100</code>. The one thing it can&rsquo;t do is
                restart your own app after a merge; that needs the host install.
              </p>
              <pre><code>{`git clone ${REPO}.git && cd persei
cp docker/.env.example .env
docker compose up -d`}</code></pre>
              <p className="lp-reqs">Docker &middot; an OpenRouter API key</p>
            </article>
          </div>
          <p className="lp-sub lp-install-note">
            <strong>Mac:</strong> not yet. It&rsquo;s on the list, and the newsletter is where
            I&rsquo;ll say when it&rsquo;s ready. You don&rsquo;t need a domain for any of this
            either: the agent listens on localhost, and an SSH tunnel is enough to reach it from
            somewhere else.
          </p>
          <div className="lp-cta">
            <a
              className="lp-btn lp-btn-quiet"
              href={`${REPO}/releases/latest`}
              target="_blank"
              rel="noopener noreferrer"
            >
              Download the latest release
            </a>
            <a className="lp-btn lp-btn-quiet" href={REPO} target="_blank" rel="noopener noreferrer">
              <GitHubMark />
              View the source
            </a>
          </div>
        </section>

        {/* A plain HTML form: method, action, two fields. No fetch, no
            handler, no JavaScript -- the browser posts it and follows the
            redirect the server answers with, which is what keeps this page
            static. site/server/newsletter.py is the other end. */}
        <section className="lp-section lp-news" id="newsletter">
          <h2 className="lp-h2">What changed, and what&rsquo;s next</h2>
          <p className="lp-sub">
            Every release comes with a changelog that says what changed and why. The newsletter
            is that, plus what I&rsquo;m building next and what turned out to be a bad idea. I
            only send it when there&rsquo;s something worth reading, and I never send anything
            else to this list.
          </p>
          <form className="lp-news-form" method="post" action={SUBSCRIBE_URL}>
            <div className="lp-news-fields">
              <label className="lp-field">
                <span>Name</span>
                <input
                  type="text"
                  name="name"
                  autoComplete="name"
                  maxLength={120}
                  required
                  placeholder="Ada Lovelace"
                />
              </label>
              <label className="lp-field">
                <span>Email</span>
                <input
                  type="email"
                  name="email"
                  autoComplete="email"
                  maxLength={254}
                  required
                  placeholder="ada@example.com"
                />
              </label>
            </div>
            {/* Honeypot: off-screen, out of the tab order, and never filled by a
                person. The server drops a submission that has it (2026-09-29
                audit, S2). The name is deliberately ordinary. */}
            <label className="lp-hp" aria-hidden="true">
              <span>Website</span>
              <input type="text" name="website" tabIndex={-1} autoComplete="off" />
            </label>
            <button className="lp-btn lp-btn-primary lp-news-submit" type="submit">
              Sign up for the newsletter
            </button>
            <p className="lp-news-fine">
              I keep your name and email to send you the newsletter, nothing else. Never sold,
              never shared.
            </p>
          </form>
        </section>
      </main>

      <footer className="lp-foot">
        <div className="lp-foot-brand">
          <img src={logoUrl} alt="Persei" />
        </div>
        <p>
          Source available under PolyForm Noncommercial 1.0.0 &mdash; free for any noncommercial
          use. Built and maintained by me, Danny,{" "}
          <a href={`mailto:${OWNER_EMAIL}`}>{OWNER_EMAIL}</a>.
        </p>
        <a href={REPO} target="_blank" rel="noopener noreferrer">
          <GitHubMark />
          github.com/DJG3DK/persei
        </a>
      </footer>
    </div>
  );
}
