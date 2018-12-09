#!/usr/bin/python3

import threading
import time
import re
import requests
import asyncio
import aiohttp
import json.decoder
import html
import shelve
from functools import wraps, partial
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, ChatAction
from telegram.ext import (Updater, CommandHandler, MessageHandler, Filters, CallbackQueryHandler,
                          PicklePersistence)

# auth token for the telegram bot; get this from @BotFather
TELEGRAM_TOKEN = "FIXME"

# file to store persistent user-specific data in
PERSISTENCE_USER_FILENAME = 'rta-user.data'

# file to store persistent global data in
PERSISTENCE_GLOBAL_SNS_FILENAME = 'rta-global.data'

# URL to graft supernodes.  Should not end in a /
SUPERNODES = [
        ('Jas1', 'http://localhost:29001'),
        ('Jas8', 'http://localhost:29008'),
        ('JasG', 'http://localhost:29016'),
        ('dev1', 'http://18.214.197.224:28690'),
        ('dev2', 'http://18.214.197.50:28690'),
        ('dev3', 'http://35.169.179.171:28690'),
        ('dev4', 'http://34.192.115.160:28690'),
]

# URL(s) to graft nodes; typically the one the above RTA_URL supernode is connected to.  The first
# one is used when we need some info (like current height); they all get used for things like the
# /nodes and /height commands.
NODES = [
        ('J0', 'http://localhost:28681'),
        ('J1', 'http://localhost:55111'),
        ('J2', 'http://localhost:55001'),
        ('dev1', 'http://18.214.197.224:28681'),
        ('dev2', 'http://18.214.197.50:28681'),
        ('dev3', 'http://35.169.179.171:28681'),
        ('dev4', 'http://34.192.115.160:28681'),
]

# Telegram handle of the bot's owner
OWNER = 'FIXME'

# Warn about a SN going offline if the last uptime becomes greater than this many minutes
TIMEOUT = 3600

# Send out a summary of online nodes at most once every (this number) seconds
SUMMARY_FREQUENCY = 3600

# Channel/group chat id to send updates to
SEND_TO = 'FIXME'

# URL to a wallet rpc for the /send and /balance commands (no trailing /)
WALLET_RPC = 'http://localhost:55115'

# Authorized users for restricted commands (e.g. /send)
# When an unauthorized user sends a /send message, the userid will be printed to stdout
BOSS_USERS = {
# 12345: '@some_user',
}

# Enable to broadcast "I'm alive" upon startup
ANNOUNCE_LIFE = False




# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)

logger = logging.getLogger(__name__)

pp = None
globalsns = None
lastresults = None
updater = None

print = partial(print, flush=True)

def tier(balance):
    return (0 if balance <  500000000000000 else
            1 if balance <  900000000000000 else
            2 if balance < 1500000000000000 else
            3 if balance < 2500000000000000 else
            4)


def format_wallet(addr):
    return '*' + addr[0:6] + '...' + addr[-3:] + '*'


def format_balance(atoms):
    bal = atoms * 1e-10
    if bal >= 1000000:
        return "{:.2f}M".format(bal / 1000000)
    if bal >= 100000:
        return "{:.0f}K".format(bal / 1000)
    if bal >= 10000:
        return "{:.1f}K".format(bal / 1000)
    if bal >= 1000:
        return "{:.2f}K".format(bal / 1000)
    if bal >= 100:
        return "{:.0f}".format(bal)
    if bal >= 10:
        return "{:.1f}".format(bal)
    if bal >= 1:
        return "{:.2f}".format(bal)
    return "{:.4f}".format(bal)


def format_tier(t):
    return ('_[t₀]_', '_[t₁]_', '_[t₂]_', '_[t₃]_', '_[t₄]_')[t]


def friendly_ago(ago):
    ago = int(ago)
    seconds = ago % 60
    ago //= 60
    minutes = ago % 60
    ago //= 60
    hours = ago % 24
    ago //= 24
    days = ago
    return ('{}d{}h{:02d}m{:02d}s'.format(days, hours, minutes, seconds) if days else
            '{}h{:02d}m{:02d}s'.format(hours, minutes, seconds) if hours else
            '{}m{:02d}s'.format(minutes, seconds) if minutes else
            '{}s'.format(seconds))


def send_reply(bot, update, message, reply_to=None, reply_markup=None, parse_mode=ParseMode.MARKDOWN):
    if reply_to is None:
        reply_to = update.message
    if reply_to:
        reply_to.reply_text(message, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        bot.send_message(
            chat_id=update.callback_query.message.chat_id,
            text=message, parse_mode=parse_mode,
            reply_markup=reply_markup)


def restricted(func):
    @wraps(func)
    def wrapped(bot, update, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in BOSS_USERS:
            print("Unauthorized access denied for {}.".format(user_id))
            send_reply(bot, update, "I'm sorry, Dave.  I'm afraid I can't do that. (You aren't authorized to send funds! — " +
                    " ".join(BOSS_USERS.values()) + " 👆)");
            return
        return func(bot, update, *args, **kwargs)
    return wrapped


def send_action(action):
    """Sends `action` while processing func command."""

    def decorator(func):
        @wraps(func)
        def command_func(*args, **kwargs):
            bot, update = args
            bot.send_chat_action(chat_id=update.message.chat_id, action=action)
            func(bot, update, **kwargs)
        return command_func

    return decorator


def needs_data(func):
    @wraps(func)
    def wrapped(bot, update, *args, **kwargs):
        global lastresults
        if not lastresults:
            send_reply(bot, update, 'I\'m still starting up; try again later')
            return
        return func(bot, update, *args, **kwargs)
    return wrapped


eighths = ' ▏▎▍▌▋▊▉█'

def get_dist(results = None):
    global globalsns, lastresults
    if results is None:
        results = lastresults
    num_tiers = [0, 0, 0, 0, 0]
    total_balance, total_stakes = 0, 0
    now = time.time()
    for a, g in globalsns.items():
        if g['last_seen'] and g['last_seen'] >= now - TIMEOUT:
            t = g['tier']
            num_tiers[t] += 1
            total_balance += g['stake']
            total_stakes += (50 if t == 1 else 90 if t == 2 else 150 if t == 3 else 250 if t == 4 else 0)
    total_stakes *= 10000000000000

    num_sns = sum(num_tiers[1:])
    if num_sns == 0:
        return 'I\'m still starting up; try again later'
    num_offline = len(globalsns) - num_sns
    pct_tiers = [x / num_sns * 100 for x in num_tiers[1:]]
    global eighths
    blocks = []
    for i in range(len(pct_tiers)):
        x = pct_tiers[i] * 2
        blocks.append('█' * int(x // 8))
        e = round(x % 8)
        if e > 0:
            blocks[-1] += eighths[e]
    dist = "*Supernode distribution:*\n"
    for t in (1, 2, 3, 4):
        dist += ('T{}: ' + blocks[t-1] + " ({} = {:.1f}%)\n").format(t, num_tiers[t], pct_tiers[t-1])
    dist += '{} supernode{} online and staked\n'.format(num_sns, 's' if num_sns != 1 else '')
    now = time.time()
    dist += 'Uptimes: *{}* ≤ _2m_, *{}* ≤ _10m_, *{}* ≤ _1h_\n'.format(
            sum(bool(x['last_seen'] and now - x['last_seen'] <= 2*60) for x in globalsns.values()),
            sum(bool(x['last_seen'] and 2*60 < now - x['last_seen'] <= 10*60) for x in globalsns.values()),
            sum(bool(x['last_seen'] and 10*60 < now - x['last_seen'] <= 60*60) for x in globalsns.values()))
    num_queried = sum(bool(x) for x in results.values())
    online_on = { 'all': 0, 'most': 0, 'some': 0, 'one': 0 }
    for a in globalsns.keys():
        count = sum(a in r and r[a]['LastUpdateAge'] < TIMEOUT for r in results.values() if r)
        if count == 0:
            continue
        key =  ('all' if count == num_queried else
                'most' if count >= 0.5*num_queried else
                'some' if count > 1 else 'one')
        online_on[key] += 1

    dist += 'Network:\n_{all}_/_{most}_/_{some}_/_{one}_ SNs online on all/most/some/one queried SNs\n'.format(**online_on)

    if num_tiers[0] > 0:
        dist += '{} supernode{} online with < T1 stake\n'.format(num_tiers[0], 's' if num_tiers[0] != 1 else '')
    dist += '{} supernode{} offline'.format(num_offline, 's' if num_offline != 1 else '')

    dist += '\nOnline stakes: *{}* (*{}* required)'.format(
            format_balance(total_balance), format_balance(total_stakes))
    return dist


@needs_data
def show_dist(bot, update, user_data):
    send_reply(bot, update, get_dist())


async def get_json_data(urls, timeout=10):
    results = [None] * len(urls)
    async with aiohttp.ClientSession(read_timeout=timeout) as session:
        for i in range(len(urls)):
            try:
                async with session.get(urls[i]) as resp:
                    results[i] = await resp.json()
            except (json.decoder.JSONDecodeError,
                    aiohttp.ClientError,
                    asyncio.TimeoutError) as e:
                print("Something getting wrong during json data fetching: {}".format(e))
    return results


time_to_die = False
def rta_updater():
    global lastresults, time_to_die, updater
    last = 0
    first = ANNOUNCE_LIFE
    last_summary = time.time()

    loop = asyncio.new_event_loop()

    while not time_to_die:
        if time.time() - last < 60:
            time.sleep(0.25)
            continue

        results = loop.run_until_complete(get_json_data([
            sn[1] + '/debug/supernode_list/1' for sn in SUPERNODES]))

        results = dict(zip(
            (s[0] for s in SUPERNODES),
            ({ x['Address']: x for x in r['result']['items'] } if r else None for r in results)
        ))

        if not any(results.values()):
            print("Something getting very wrong: all SNs returned nothing!")
            time.sleep(3)
            continue

        now = time.time()
        last = now

        # Each of these contain: { 'addr': 'F...', 'age': 123, 'tier': [0-4], 'old_tier': [0-4] }
        # (old_tier is only set if the SN was seen last time and the tier has changed)
        new_addr = set() 
        new_sns = []
        timeouts = []
        returns = []
        tier_was = {}
        went_offline = set()
        came_online_after = {}
        num_sns = 0

        now = time.time()
        for sn in SUPERNODES:
            sn_tag = sn[0]
            stats = results[sn_tag]
            if not stats:
                continue
            num_sns += 1

            for x in stats.values():
                a = x['Address']
                seen = None if x['LastUpdateAge'] > 1000000000 else now - x['LastUpdateAge']
                if a not in globalsns:
                    globalsns[a] = {}
                    new_addr.add(a)
                g = globalsns[a]
                update_stake = 'stake' not in g
                if seen and ('last_seen' not in g or g['last_seen'] is None or seen > g['last_seen']):
                    g['last_seen'] = seen
                    update_stake = True
                if update_stake:
                    t = tier(x['StakeAmount'])
                    if 'tier' in g and t != g['tier']:
                        tier_was[a] = g['tier']
                    g['tier'] = t
                    g['stake'] = x['StakeAmount']

        for a, g in globalsns.items():
            for k in ('last_seen', 'tier'):
                if k not in g:
                    g[k] = None
            if g['last_seen'] is None or g['last_seen'] < now - TIMEOUT:
                if 'online_since' in g:
                    del g['online_since']
                    went_offline.add(a)
                if 'offline_since' not in g:
                    g['offline_since'] = now if g['last_seen'] is None else g['last_seen'] + TIMEOUT
            else:
                if 'offline_since' in g:
                    came_online_after[a] = now - g['offline_since']
                    del g['offline_since']
                if 'online_since' not in g:
                    g['online_since'] = g['last_seen']

            seen, t = g['last_seen'], g['tier']
            seen_by = [x[0] for x in SUPERNODES if x[0] in results and results[x[0]] and a in results[x[0]]]
            age = None if seen is None else now - seen
            row = { 'addr': a, 'age': age, 'tier': t, 'seen_by': seen_by }
            if a in new_addr:
                if age is not None and age < TIMEOUT:
                    new_sns.append(row)
                continue
            elif a in came_online_after:
                returns.append(row)
            elif a in went_offline:
                timeouts.append(row)

        updates = []
        if first:
            updates.append("I'm alive! 🍚 🍅 🍏")
        for x in new_sns:
            updates.append("💖 New *T{}* supernode appeared: {}".format(x['tier'], format_wallet(x['addr'])))
        for a, old_tier in tier_was.items():
            if globalsns[a]['tier'] > old_tier:
                updates.append("💰 {} upgraded from *T{}* to *T{}*".format(format_wallet(x['addr']), x['old_tier'], x['tier']))
            elif globalsns[a]['tier'] < old_tier:
                updates.append("😢 {} downgraded from *T{}* to *T{}*".format(format_wallet(x['addr']), x['old_tier'], x['tier']))
        for x in returns:
            updates.append("💓 {} is back online _(after {})_!".format(
                format_wallet(x['addr']),
                'unknown' if x['addr'] not in came_online_after else friendly_ago(came_online_after[x['addr']])))
        for x in timeouts:
            updates.append("💔 {} is offline!".format(format_wallet(x['addr'])))

        if now - last_summary >= SUMMARY_FREQUENCY:
            updates.append(get_dist(results))
            last_summary = now

        if updates:
            try:
                updater.bot.send_message(SEND_TO, '\n'.join(updates), parse_mode=ParseMode.MARKDOWN)
                first = False
            except Exception as e:
                print("An exception occured during updating/notifications: {}".format(e))
                continue
        lastresults = results


def start(bot, update, user_data):
    reply_text = 'Hi!  I\'m here to help manage the RTA group with status updates and sending stakes.  I can also look up an individual SN for you: just send me the address.'
    reply_text += '\n(If I break, you should contact ' + OWNER + ')\n'
    reply_text += '''
Supported commands:

ADDR — queries the status of the given SN from the point of view of the bot's local SN.  The address should be specified by itself in a direct message or in a direct reply to bot in the group.  The address can also be specified in a shortened form such as {}

/sn ADDR — same as the above, but the bot responds in the group (without needing to use a direct reply).

/dist — shows the current active SN distribution across tiers.

/sample — shows the auth sample for the current or a requested block.

/nodes — shows the status of the graft nodes this bot talks to.

/snodes — shows the status of the graft supernodes this bot talks to.

/height — shows the current height (or heights) on the nodes this bot talks to.

/balance — shows the balance of the bot's wallet for sending out test stakes.

/donate — shows the bot's address (to help replenish the bot's available funds).

/send T1 WALLET — requests that the bot sends a stake.  If you aren't an authorized user this tags authorized users to send the stake.  For authorized users, this sends the stake.  Multiple stakes can be send at once using `/send T1 WALLET1 T2 WALLET1`.  Specific amounts can be sent using an amount instead of a `T1` tier.

/send — can be used by an authorized user in reply to a previously denied `/send ...` command to instruct the bot to go ahead with the stake request.
'''.format(format_wallet("F8JasZ9gHUSV8ir1pVpv5UB1o1xpcCF5RRpViDTbXVAwACQwXGQXL9EFbhQyv2mRpzEopBNf28kV3NWriLJGBzvJ4RR3L5Q"))

    send_reply(bot, update, reply_text)


def sn_info(addr):
    global globalsns, lastresults
    if addr not in globalsns:
        return 'Sorry, I have never seen that supernode. 🙁'
    else:
        sn = globalsns[addr]
        if not sn['last_seen']:
            if 'funded' in sn and sn['funded']:
                return 'I sent {} to that SN ({}), but I have never seen it online.'.format(
                        format_balance(sum(sn['funded'].values())),
                        ', '.join('[{}...](https://rta.graft.observer/tx/{})'.format(x[0:8], x) for x in sn['funded'].keys())
                        )
            else:
                return 'Sorry, I have never seen that supernode. 🙁'
        now = time.time()
        msgs = []
        msgs.append("*Tier:* {} (_{}.{:010d} GRFT_)".format(sn['tier'], sn['stake'] // 10000000000, sn['stake'] % 10000000000))
        msgs.append("*Last announce:* {} ago".format(friendly_ago(now - sn['last_seen'])))
        online_for = [sn for sn, r in lastresults.items() if r and addr in r and r[addr]['LastUpdateAge'] <= TIMEOUT]
        offline_for = [sn for sn, r in lastresults.items() if r and (addr not in r or r[addr]['LastUpdateAge'] > TIMEOUT)]
        mixed = online_for and offline_for
        if 'online_since' in sn or mixed:
            msgs.append("*Status:* 💓 online")
            if 'online_since' in sn:
                msgs[-1] += " _({})_".format(friendly_ago(now - sn['online_since']))
            if mixed:
                msgs[-1] += ' — ('+', '.join('_'+x+'_' for x in online_for)+')'
        if 'offline_since' in sn or mixed:
            msgs.append("*Status:* 💔 offline")
            if 'offline_since' in sn:
                msgs[-1] += " _({})_".format(friendly_ago(now - sn['offline_since']))
            if mixed:
                msgs[-1] += ' — ('+', '.join('_'+x+'_' for x in offline_for)+')'
        if 'funded' in sn:
            msgs.append("*Funding:* {} ({})".format(
                format_balance(sum(sn['funded'].values())),
                ', '.join('[{}](https://rta.graft.observer/tx/{})'.format(x[0:8], x) for x in sn['funded'].keys())
            ))
        return '\n'.join(msgs)

RE_ADDR = r'F[3-9A-D][1-9a-km-zA-HJ-NP-Z]{93}'
RE_ADDR_PATTERN = r'(F[3-9A-D][1-9a-km-zA-HJ-NP-Z]{3,})\.\.\.([1-9a-km-zA-HJ-NP-Z]{0,})'


@needs_data
def show_sn(bot, update, user_data, args):
    global globalsns
    addrs = []
    replies = []
    for a in args:
        if re.fullmatch(RE_ADDR , a):
            send_reply(bot, update, sn_info(a))
            continue

        m = re.fullmatch(RE_ADDR_PATTERN, a)
        if m:
            prefix, suffix = m.group(1), m.group(2)
            found = False
            for x in globalsns.keys():
                if x.startswith(prefix) and x.endswith(suffix):
                    found = True
                    send_reply(bot, update, sn_info(x))

            if not found:
                send_reply(bot, update, 'Sorry, but I don\'t know of any SNs like *{}*! 🙁'.format(a))
        else:
            send_reply(bot, update, 'That doesn\'t look like a valid testnet address')


def filter_nodes(args, select_from=NODES, empty_means_all=True):
    if not args and empty_means_all:
        return (select_from, [])
    ns, leftover = [], []
    for want_n in args:
        found = False
        for n in select_from:
            if want_n == n[0]:
                ns.append(n)
                found = True
                break
        if not found:
            leftover.append(want_n)

    return (ns, leftover)


@needs_data
@send_action(ChatAction.UPLOAD_DOCUMENT)
def show_sample(bot, update, user_data, args):
    height, relative = None, False
    if len(args) >= 1 and re.match(r'^[+-]?\d+$', args[0]):
        relative = args[0][0] in ('+', '-')
        height = int(args[0])
        args = args[1:]
    sns, leftover = filter_nodes(args, select_from=SUPERNODES)
    if leftover:
        send_reply(bot, update, "❌ Bad arguments!\nUsage: /sample [HEIGHT|+N|-N] [SN ...]"
                "— shows auth sample for the current height, the given height, or current height ±N")
        return

    try:
        curr_height = requests.get(NODES[0][1] + '/getheight').json()['height']
    except Exception as e:
        send_reply(bot, update, "⚠ *Something getting wrong* while getting current height 💩")
        raise e

    if height is None:
        height = curr_height
    elif relative:
        height = curr_height + height
        if height < 0:
            send_reply(bot, update, "😝 I can't look up a block before the genesis block!")
            return

    if height >= curr_height + 20:
        send_reply(bot, update, "🔮 Error: crystal ball malfunctioned; can't look up more than 19 blocks into the future")
        return

    loop = asyncio.new_event_loop()
    results = loop.run_until_complete(get_json_data([
        sn[1] + '/debug/auth_sample/{}'.format(height) for sn in sns], timeout=2))
    samples = {}
    for sn, r in zip(sns, results):
        if not r:
            continue
        sample = ' '.join(
                "{}{}".format(format_wallet(sn['Address']), format_tier(tier(sn['StakeAmount']))) for sn in r['result']['items'])
        if sample not in samples:
            samples[sample] = []
        samples[sample].append(sn[0])

    msg = None
    if not samples:
        msg = "⚠ *Something getting wrong* while getting auth samples 💩"
    elif len(samples) > 1:
        msg = '\n'.join(s+' — ('+', '.join('_'+x+'_' for x in h)+')' for s, h in samples.items())
    else:
        msg = next(iter(samples.keys()))

    send_reply(bot, update, 'Auth sample for height _{}_:\n'.format(height) + msg)


@send_action(ChatAction.UPLOAD_DOCUMENT)
def show_height(bot, update, user_data, args):
    ns, leftover = filter_nodes(args)
    if leftover:
        send_reply(bot, update, "❌ {} isn't a node I know about".format(leftover[0]))
        return

    heights = {}
    loop = asyncio.new_event_loop()
    results = loop.run_until_complete(get_json_data([
        n[1] + '/getheight' for n in ns], timeout=2))
    for n, r in zip(ns, results):
        if not r:
            continue
        h = r['height']
        if h not in heights:
            heights[h] = []
        heights[h].append(n[0])

    msg = None
    if not heights:
        msg = "⚠ *Something getting wrong* while getting current height 💩"
    elif len(heights) > 1:
        msg = "Current heights:" + ''.join(
                "\n*{}* ({})".format(h, ', '.join("_"+x+"_" for x in heights[h]))
                for h in sorted(heights.keys()))
    else:
        msg = "Current height: *{}*".format(next(iter(heights.keys())))

    send_reply(bot, update, msg)


@send_action(ChatAction.UPLOAD_DOCUMENT)
def show_nodes(bot, update, user_data, args):
    ns, leftover = filter_nodes(args)
    if leftover:
        send_reply(bot, update, "❌ {} isn't a node I know about".format(leftover[0]))
        return

    heights = {}
    loop = asyncio.new_event_loop()
    results = loop.run_until_complete(get_json_data([
        n[1] + '/getinfo' for n in ns], timeout=2))
    status = []
    for n, r in zip(ns, results):
        st = None
        if not r:
            st = "*{}*: Connection failed 💣".format(n[0])
        else:
            st = "*{}*: H:*{}*; *{}*_(out)_+*{}*_(in)_; up *{}*".format(
                    n[0],
                    r['height'], r['outgoing_connections_count'], r['incoming_connections_count'],
                    friendly_ago(time.time() - r['start_time']))
        status.append(st)

    send_reply(bot, update, '\n'.join(status))


@needs_data
def show_snodes(bot, update, user_data, args):
    global lastresults
    sns, leftover = filter_nodes(args, select_from=SUPERNODES)
    if leftover:
        send_reply(bot, update, "❌ {} isn't a supernode I know about".format(leftover[0]))
        return

    stats = []
    for sn in sns:
        st = '*{}*: '.format(sn[0])
        r = lastresults[sn[0]]
        if not r:
            st += '_connection failed_'
            continue
        count = { x: 0 for x in ('2m', '10m', '1h', 'online', 'offline', 'never') }
        for t in range(5):
            count['t{}'.format(t)] = 0

        for x in r.values():
            if x['LastUpdateAge'] <= TIMEOUT:
                count['online'] += 1
                count['t{}'.format(tier(x['StakeAmount']))] += 1
                if x['LastUpdateAge'] <= 120:
                    count['2m'] += 1
                elif x['LastUpdateAge'] <= 600:
                    count['10m'] += 1
                else:
                    count['1h'] += 1
            elif x['LastUpdateAge'] <= 1000000000:
                count['offline'] += 1
            else:
                count['never'] += 1

        st += '*{online}* 💓, *{offline}* 💔, *{never}* 🛑'.format(**count)
        if count['online'] > 0:
            st += '  _({2m}/{10m}/{1h})_  *[{t1}-{t2}-{t3}-{t4}]*'.format(**count)

        stats.append(st)

    stats.append("Legend: 💓=online; 💔=expired; 🛑=offline; _(a/b/c)_=2m/10m/1h uptime counts; *[w-x-y-z]*={}-…-{} counts".format(
        format_tier(1), format_tier(4)))

    send_reply(bot, update, '\n'.join(stats))




@needs_data
def msg_input(bot, update, user_data):
    global globalsns

    public_chat = update.message.chat.type != 'private'
    if re.fullmatch(RE_ADDR, update.message.text):
        send_reply(bot, update, sn_info(update.message.text))
        return

    addrs = []
    m = re.fullmatch(RE_ADDR_PATTERN, update.message.text)
    if m:
        prefix, suffix = m.group(1), m.group(2)
        for x in globalsns.keys():
            if x.startswith(prefix) and x.endswith(suffix):
                addrs.append(x)

        if not addrs:
            send_reply(bot, update, 'Sorry, but I don\'t know of any SNs matching that pattern! 🙁')

    else:
        if not public_chat:
            send_reply(bot, update, 'That doesn\'t look like a testnet address.  Send me a SN wallet address to query my stats')
        return

    if len(addrs) == 1:
        send_reply(bot, update, sn_info(addrs[0]))
    else:
        send_reply(bot, update, '\n\n'.join('{}:\n{}'.format(format_wallet(x), sn_info(x)) for x in addrs))


already_sent = set()

@restricted
@send_action(ChatAction.TYPING)
def send_stake(bot, update, user_data, args):

    bad = None
    dest = []
    amounts = {
            'T1':  505000000000000,
            'T2':  905000000000000,
            'T3': 1505000000000000,
            'T4': 2505000000000000
    }
    reply_to = update.message
    if not args and update.message.reply_to_message:
        rep = update.message.reply_to_message
        m = re.match(r'^/send(?:@\S+)?((?:\s+(?:\d+(?:\.\d+)?|[Tt][1-4])\s+' + RE_ADDR + r')+)\s*$', rep.text)
        if m:
            args = m.group(1).split()
            reply_to = rep

    if reply_to.message_id in already_sent:
        # Don't resend a duplicate /send message
        send_reply(bot, update, "🔴 I'm sorry, Dave, I already opened the pod bay doors 🙁");
        return
    already_sent.add(reply_to.message_id)

    append_usage = "\nUsage: /send {NNN,T1,T2,T3,T4} WALLET [TIER WALLET [...]]"
    stake_details = []
    if len(args) < 2 or len(args) % 2 != 0:
        bad = 'Wrong number of arguments'
    else:
        for i in range(0, len(args), 2):
            tier, wallet = args[i:i+2]
            if not re.fullmatch(RE_ADDR, wallet):
                bad = '{} does not look like a valid testnet wallet address'.format(wallet)
                break

            amount = 0
            if re.match(r'^\d+(?:\.\d+)?$', tier):
                amount = int(float(tier) * 1e10)
                if amount > amounts['T4']:
                    bad = 'Sorry; {} is too much 💰 to send all at once'.format(tier)
                    break
                stake_details.append(format_wallet(wallet) + ' 👈 ' + format_balance(amount))
            elif tier.upper() in amounts:
                staked_already = (sum(globalsns[wallet]['funded'].values())
                        if wallet in globalsns and 'funded' in globalsns[wallet] else 0)
                amount = amounts[tier.upper()] - staked_already
                if amount > 0:
                    stake_details.append(format_wallet(wallet) + ' 👈 ' + format_balance(amount) + (' more' if staked_already else ''))
                else:
                    bad = "🔴 I'm sorry, Dave, I'm afraid I can't do that: I already sent " + format_balance(staked_already) + " to " + format_wallet(wallet)
                    append_usage = ''
                    break
            else:
                bad = 'Invalid amount to send: _{}_'.format(tier)
                break
            dest.append({"amount": amount, "address": wallet})

    if bad is not None:
        send_reply(bot, update, bad + append_usage,
                reply_to=reply_to)
        return

    assert(len(dest) > 0)

    try:
        data = requests.post(WALLET_RPC + '/json_rpc', timeout=5,
                json={
                    "jsonrpc":"2.0","id":"0","method":"transfer","params":{
                        "destinations": dest,
                        "priority": 1,
                    }
                }).json()
        if 'error' in data and data['error']:
            print("transfer error occured: {}".format(data['error']['message']))
            reply = "⚠ <b>Something getting wrong</b> while sending payment:\n<i>{}</i>".format(
                    html.escape(data['error']['message']))
            send_reply(bot, update, reply, parse_mode=ParseMode.HTML, reply_to=reply_to)
        else:
            tx_hash = data['result']['tx_hash']
            print("Sent stakes:")
            for x in dest:
                addr, amt = x['address'], x['amount']
                print('\n    {} -- {}'.format(addr, amt))
                if addr not in globalsns:
                    globalsns[addr] = {}
                if 'funded' not in globalsns[addr]:
                    globalsns[addr]['funded'] = {}
                globalsns[addr]['funded'][tx_hash] = amt
            send_reply(bot, update, "💸 Stake{} sent in [{}...](https://rta.graft.observer/tx/{}):\n{}".format(
                    '' if len(dest) == 1 else 's',
                    tx_hash[0:8], tx_hash, '\n'.join(stake_details)),
                reply_to=reply_to)
    except Exception as e:
        print("An exception occured while sending:")
        print(e)
        send_reply(bot, update, "⚠ *Something getting wrong* while sending payment 💩", reply_to=reply_to)
        raise e


@send_action(ChatAction.TYPING)
def balance(bot, update, user_data):
    try:
        data = requests.post(WALLET_RPC + '/json_rpc', timeout=2,
                json={"jsonrpc":"2.0","id":"0","method":"getbalance"}).json()['result']
        balance, unlocked = data["balance"], data["unlocked_balance"]
    except Exception as e:
        print("An exception occured while fetching the balance:")
        print(e)
        send_reply(bot, update, "⚠ *Something getting wrong* while fetching wallet balance 💩")
        return
    msg = "💰 *{}* total".format(format_balance(balance))
    if balance > unlocked:
        msg += " (*{}* unlocked)".format(format_balance(unlocked))
    send_reply(bot, update, msg)


@send_action(ChatAction.TYPING)
def donate(bot, update, user_data):
    try:
        addr = requests.post(WALLET_RPC + '/json_rpc', timeout=2,
                json={"jsonrpc":"2.0","id":"0","method":"getaddress"}).json()['result']['address']
    except Exception as e:
        print("An exception occured while fetching the address:")
        print(e)
        send_reply(bot, update, "⚠ *Something getting wrong* while fetching wallet address 💩")
        return
    send_reply(bot, update, "Send donations of unneeded RTA testnet GRFT to *" + addr + "*")


rta_thread = None
def start_rta_update_thread():
    global rta_thread, lastresults
    rta_thread = threading.Thread(target=rta_updater)
    rta_thread.start()
    while True:
        if lastresults and any(lastresults):
            print("Initial RTA stats fetched")
            return
        print("Waiting for initial RTA stats")
        time.sleep(0.5)


def stop_rta_thread(signum, frame):
    global time_to_die, rta_thread
    time_to_die = True
    rta_thread.join()


def error(bot, update, error):
    """Log Errors caused by Updates."""
    logger.warning('Update "%s" caused error "%s"', update, error)


def main():
    print("Starting bot")
    global pp, updater, globalsns

    globalsns = shelve.open(PERSISTENCE_GLOBAL_SNS_FILENAME, writeback=True)

    # Create the Updater and pass it your bot's token.
    pp = PicklePersistence(filename=PERSISTENCE_USER_FILENAME, store_user_data=True, store_chat_data=False, on_flush=True)
    updater = Updater(TELEGRAM_TOKEN, persistence=pp,
            user_sig_handler=stop_rta_thread)

    start_rta_update_thread()

    # Get the dispatcher to register handlers
    dp = updater.dispatcher

    updater.dispatcher.add_handler(CommandHandler('start', start, pass_user_data=True))
    updater.dispatcher.add_handler(CommandHandler('dist', show_dist, pass_user_data=True))
    updater.dispatcher.add_handler(CommandHandler('send', send_stake, pass_user_data=True, pass_args=True))
    updater.dispatcher.add_handler(CommandHandler('balance', balance, pass_user_data=True))
    updater.dispatcher.add_handler(CommandHandler('donate', donate, pass_user_data=True))
    updater.dispatcher.add_handler(CommandHandler('sn', show_sn, pass_user_data=True, pass_args=True))
    updater.dispatcher.add_handler(CommandHandler('sample', show_sample, pass_user_data=True, pass_args=True))
    updater.dispatcher.add_handler(CommandHandler('height', show_height, pass_user_data=True, pass_args=True))
    updater.dispatcher.add_handler(CommandHandler('nodes', show_nodes, pass_user_data=True, pass_args=True))
    updater.dispatcher.add_handler(CommandHandler('snodes', show_snodes, pass_user_data=True, pass_args=True))
    updater.dispatcher.add_handler(MessageHandler(Filters.text, msg_input, pass_user_data=True))

    # log all errors
    dp.add_error_handler(error)

    # Start the Bot
    updater.start_polling()

    print("Bot started")

    # Run the bot until you press Ctrl-C or the process receives SIGINT,
    # SIGTERM or SIGABRT. This should be used most of the time, since
    # start_polling() is non-blocking and will stop the bot gracefully.
    updater.idle()

    print("Saving persistence and shutting down")
    pp.flush()
    globalsns.close()


if __name__ == '__main__':
    main()
