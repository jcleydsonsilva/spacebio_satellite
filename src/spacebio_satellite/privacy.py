"""What leaves your computer. This text mirrors section 9 of docs/CONTRACT.md and is shown by ``spacebio-satellite privacy``."""

SENT = [
    "the access token (so SpaceBio knows which device sent the work)",
    "public paper records found in public databases (title, abstract, authors, journal, year, language, DOI or PubMed ID, full-text links)",
    "this program's name and version, and your operating system family (for example 'Darwin' or 'Linux')",
    "the device label you typed at login (for example 'Home laptop')",
]
NOT_SENT = [
    ("your IP address is not kept with your account or your contributions (like any website, SpaceBio's servers and the protection layer in front of them see it "
     "while answering, and the counters that limit abuse hold it for at most an hour)"),
    "no file names, folders, other programs, user name or host name",
    "no API keys of yours (they are only added to requests that go straight to the public databases)",
    "your personal e-mail (the databases only ever see the PROJECT's contact address, taken from the task)",
]
STAYS = [
    "the access token, in your system keyring or in a private file",
    "the program works until you press Ctrl-C or reach the limit you set (--for, --max-tasks); SpaceBio never starts it for you",
]
KEPT = [
    "for each computer: its name, the program version, the system family, when it was last used and a one-way fingerprint of the token (never the token itself)",
    "the batches you send are deleted once checked; only counts are kept. Your papers and points are linked to your account",
]
DATABASES = (
    "The public databases (Europe PMC, PubMed, Crossref, OpenAlex) DO see your IP address when this program contacts them "
    "directly, exactly as when you open their websites."
)
CONTROL = [
    "Contributing is opt-in per account, separate from the browser contributions, and you can withdraw at any time on the SpaceBio website.",
    "Withdrawing consent disconnects every computer at once. 'Delete my contribution data' also removes your profile, points and submissions. The papers stay: they are public records.",
    "Three strikes (records that do not match the source) switch contributions off for your whole account, browser included.",
    "This program only ever adds records. It cannot delete, merge or edit anything, and it never posts anything anywhere.",
    "Run with --dry-run to see exactly what would be sent, without sending it.",
]


def render() -> list[str]:
    lines = ["WHAT LEAVES YOUR COMPUTER", "", "Sent to SpaceBio:"]
    lines += [f"  - {item}" for item in SENT]
    lines += ["", "Stays on your computer:"] + [f"  - {item}" for item in STAYS]
    lines += ["", "Kept by SpaceBio:"] + [f"  - {item}" for item in KEPT]
    lines += ["", "NOT sent:"] + [f"  - {item}" for item in NOT_SENT]
    lines += ["", DATABASES, "", "You stay in control:"] + [f"  - {item}" for item in CONTROL]
    return lines
