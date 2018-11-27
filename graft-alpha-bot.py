#!/usr/bin/python3

import threading
import time
import re
import requests
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
PERSISTENCE_GLOBAL_FILENAME = 'rta-global.data'

# URL to a graft supernode.  Should not end in a /
RTA_URL = 'http://localhost:29016'

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
globaldata = None
updater = None
last_stats = None

print = partial(print, flush=True)

def tier(balance):
    return (0 if balance <  500000000000000 else
            1 if balance <  900000000000000 else
            2 if balance < 1500000000000000 else
            3 if balance < 2500000000000000 else
            4)


def format_wallet(addr):
    return '*' + addr[0:8] + '...' + addr[-3:] + '*'


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
        global last_stats
        if not last_stats:
            send_reply(bot, update, 'I\'m still starting up; try again later')
            return
        return func(bot, update, *args, **kwargs)
    return wrapped


eighths = ' ▏▎▍▌▋▊▉█'

def get_dist(stats = None):
    global last_stats
    if stats is None:
        stats = last_stats
    num_tiers = [0, 0, 0, 0, 0]
    total_balance, total_stakes = 0, 0
    for x in stats.values():
        if x['LastUpdateAge'] <= TIMEOUT:
            t = tier(x['StakeAmount'])
            num_tiers[t] += 1
            total_balance += x['StakeAmount']
            total_stakes += (50 if t == 1 else 90 if t == 2 else 150 if t == 3 else 250 if t == 4 else 0)
    total_stakes *= 10000000000000

    num_sns = sum(num_tiers[1:])
    if num_sns == 0:
        return 'I\'m still starting up; try again later'
    num_offline = len(stats) - num_sns
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
    if num_tiers[0] > 0:
        dist += '{} supernode{} online with < T1 stake\n'.format(num_tiers[0], 's' if num_tiers[0] != 1 else '')
    dist += '{} supernode{} offline'.format(num_offline, 's' if num_offline != 1 else '')

    dist += '\nOnline stakes: *{}* (*{}* required)'.format(
            format_balance(total_balance), format_balance(total_stakes))
    return dist


@needs_data
def show_dist(bot, update, user_data):
    send_reply(bot, update, get_dist())


time_to_die = False
def rta_updater():
    global last_stats, time_to_die, updater
    last = 0
    first = ANNOUNCE_LIFE
    #first = True  # uncomment to debug reconnection
    last_summary = time.time()
    while not time_to_die:
        now = time.time()
        if now - last < 60:
            time.sleep(0.25)
            continue

        try:
            data = requests.get(RTA_URL + '/debug/supernode_list/1', timeout=2).json()
            stats = { x['Address']: x for x in data['result']['items'] }
            if len(stats) < 20:
                raise Exception("Expected many SNs, but found < 20")
        except Exception as e:
            print("Something getting wrong during RTA stats fetching: {}".format(e))
            time.sleep(0.25)
            continue

        uptime = None
        # Custom hack to query the uptime of the SN; this returns {"first":1542559794,"now":1542559839}
        # where `first` is the time when the timehack was first requested and `now` is the current
        # time.  This gives us a (hacky) way to tell whether the SN has at least the TIMEOUT uptime;
        # if it doesn't, we don't report timeouts/returns (because they might just be happening
        # because the SN just restarted and doesn't have a full picture yet).
        try:
            timehack = requests.get(RTA_URL + '/debug/timehack', timeout=2).json()
            uptime = timehack['now'] - timehack['first']
        except Exception:
            pass

        last = now

        # Each of these contain: { 'addr': 'F...', 'age': 123, 'tier': [0-4], 'old_tier': [0-4] }
        # (old_tier is only set if the SN was seen last time and the tier has changed)
        new_sns = [] 
        timeouts = []
        returns = []
        newtiers = []
        came_online_after = {}

        now = time.time()
        for x in stats.values():
            seen = None if x['LastUpdateAge'] > 1000000000 else now - x['LastUpdateAge']
            if x['Address'] not in globaldata:
                globaldata[x['Address']] = {}
            g = globaldata[x['Address']]
            if seen and ('last_seen' not in g or g['last_seen'] is None or seen > g['last_seen']):
                g['last_seen'] = seen
            if g['last_seen'] is None or g['last_seen'] < now - TIMEOUT:
                if 'online_since' in g:
                    del g['online_since']
                if 'offline_since' not in g:
                    g['offline_since'] = now if g['last_seen'] is None else g['last_seen'] + TIMEOUT
            else:
                if 'offline_since' in g:
                    came_online_after[x['Address']] = now - g['offline_since']
                    del g['offline_since']
                if 'online_since' not in g:
                    g['online_since'] = g['last_seen']

            g['tier'] = tier(x['StakeAmount'])
            g['stake'] = x['StakeAmount']

        if last_stats:
            for x in stats.values():
                addr, age, t = x['Address'], x['LastUpdateAge'], tier(x['StakeAmount'])
                row = { 'addr': addr, 'age': age, 'tier': t }
                if addr not in last_stats:
                    if age < TIMEOUT:
                        new_sns.append(row)
                    continue

                last_age = last_stats[addr]['LastUpdateAge']
                last_t = tier(last_stats[addr]['StakeAmount'])

                if t != last_t:
                    row['old_tier'] = last_t
                    newtiers.append(row)

                if age > TIMEOUT >= last_age and (uptime is None or uptime > TIMEOUT):
                    timeouts.append(row)
                elif age <= TIMEOUT < last_age and (uptime is None or uptime > TIMEOUT):
                    returns.append(row)
        else:
            last_stats = stats

        updates = []
        if first:
            updates.append("I'm alive! 🍚 🍅 🍏")
        for x in new_sns:
            updates.append("💖 New *T{}* supernode appeared: {}".format(x['tier'], format_wallet(x['addr'])))
        for x in newtiers:
            if x['tier'] > x['old_tier']:
                updates.append("💰 {} upgraded from *T{}* to *T{}*".format(format_wallet(x['addr']), x['old_tier'], x['tier']))
            else:
                updates.append("😢 {} downgraded from *T{}* to *T{}*".format(format_wallet(x['addr']), x['old_tier'], x['tier']))
        for x in returns:
            updates.append("💓 {} is back online _(after {})_!".format(
                format_wallet(x['addr']),
                'unknown' if x['addr'] not in came_online_after else friendly_ago(came_online_after[x['addr']])))
        for x in timeouts:
            updates.append("💔 {} is offline!".format(format_wallet(x['addr'])))

        if now - last_summary >= SUMMARY_FREQUENCY:
            updates.append(get_dist(stats))
            last_summary = now

        if updates:
            try:
                updater.bot.send_message(SEND_TO, '\n'.join(updates), parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                print("An exception occured during updating/notifications: {}".format(e))
                continue
            last_stats = stats
            first = False


def start(bot, update, user_data):
    reply_text = 'Hi!  I\'m here to help manage the RTA group with status updates and sending stakes.  I can also look up an individual SN for you: just send me the address.'
    reply_text += '\n(If I break, you should contact ' + OWNER + ')\n'
    reply_text += '''
Supported commands:

ADDR — queries the status of the given SN from the point of view of the bot's local SN.  The address should be specified by itself in a direct message or in a direct reply to bot in the group.  The address can also be specified in a shortened form such as {}

/sn ADDR — same as the above, but the bot responds in the group (without needing to use a direct reply).

/dist — shows the current active SN distribution across tiers.

/balance — shows the balance of the bot's wallet for sending out test stakes.

/donate — shows the bot's address (to help replenish the bot's available funds).

/send T1 WALLET — requests that the bot sends a stake.  If you aren't an authorized user this tags authorized users to send the stake.  For authorized users, this sends the stake.  Multiple stakes can be send at once using `/send T1 WALLET1 T2 WALLET1`.  Specific amounts can be sent using an amount instead of a `T1` tier.

/send — can be used by an authorized user in reply to a previously denied `/send ...` command to instruct the bot to go ahead with the stake request.
'''.format(format_wallet("F8JasZ9gHUSV8ir1pVpv5UB1o1xpcCF5RRpViDTbXVAwACQwXGQXL9EFbhQyv2mRpzEopBNf28kV3NWriLJGBzvJ4RR3L5Q"))

    send_reply(bot, update, reply_text)


def sn_info(addr):
    if addr not in globaldata:
        return 'Sorry, I have never seen that supernode. 🙁'
    else:
        sn = globaldata[addr]
        if 'last_seen' not in sn:
            if 'funded' in sn and sn['funded']:
                return 'I sent {} to that SN _({})_, but I have never seen it online.'.format(
                        format_balance(sum(sn['funded'].values())),
                        ', '.join('[{}](https://rta.graft.observer/tx/{})'.format(x[0:8], x) for x in sn['funded'].keys())
                        )
            else:
                return 'Sorry, I have never seen that supernode. 🙁'
        now = time.time()
        msgs = []
        msgs.append("*Tier:* {} (_{}.{:010d} GRFT_)".format(sn['tier'], sn['stake'] // 10000000000, sn['stake'] % 10000000000))
        msgs.append("*Last announce:* {} ago".format(friendly_ago(now - sn['last_seen'])))
        if 'online_since' in sn:
            msgs.append("*Status:* 💓 online _({})_".format(friendly_ago(now - sn['online_since'])))
        else:
            msgs.append("*Status:* 💔 offline _({})_".format(friendly_ago(now - sn['offline_since'])))
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
    global last_stats
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
            for x in last_stats.keys():
                if x.startswith(prefix) and x.endswith(suffix):
                    found = True
                    send_reply(bot, update, sn_info(x))

            if not found:
                send_reply(bot, update, 'Sorry, but I don\'t know of any SNs like *{}*! 🙁'.format(a))
        else:
            send_reply(bot, update, 'That doesn\'t look like a valid testnet address')


@needs_data
def msg_input(bot, update, user_data):
    global last_stats

    public_chat = update.message.chat.type != 'private'
    if re.fullmatch(RE_ADDR, update.message.text):
        send_reply(bot, update, sn_info(update.message.text))
        return

    addrs = []
    m = re.fullmatch(RE_ADDR_PATTERN, update.message.text)
    if m:
        prefix, suffix = m.group(1), m.group(2)
        for x in last_stats.keys():
            if x.startswith(prefix) and x.endswith(suffix):
                addrs.append(x)

        if not addrs:
            send_reply(bot, update, 'Sorry, but I don\'t know of any SNs matching that pattern! 🙁')

    else:
        if not public_chat:
            send_reply(bot, update, 'That doesn\'t look like a testnet address.  Send me a SN wallet address to query my stats')
        return

    if len(addrs) == 1:
        print("info: {}".format(sn_info(addrs[0])))
        send_reply(bot, update, sn_info(addrs[0]))
    else:
        print("info: {}".format(addrs))
        send_reply(bot, update, '\n\n'.join('{}:\n{}'.format(format_wallet(x), sn_info(x)) for x in addrs))


already_sent = set()

@restricted
@send_action(ChatAction.TYPING)
def send_stake(bot, update, user_data, args):
    global last_stats

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
            elif tier.upper() in amounts:
                amount = amounts[tier.upper()]
            else:
                bad = 'Invalid amount to send: _{}_'.format(tier)
                break
            dest.append({"amount": amount, "address": wallet})

    if bad is not None:
        send_reply(bot, update, bad + "\nUsage: /send {NNN,T1,T2,T3,T4} WALLET [TIER WALLET [...]]",
                reply_to=reply_to)

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
                if addr not in globaldata:
                    globaldata[addr] = {}
                if 'funded' not in globaldata[addr]:
                    globaldata[addr]['funded'] = {}
                globaldata[addr]['funded'][tx_hash] = amt
            send_reply(bot, update, "💸 Stake{} sent in [{}...](https://rta.graft.observer/tx/{})!".format(
                    '' if len(dest) == 1 else 's',
                    tx_hash[0:8], tx_hash),
                reply_to=reply_to)
    except Exception as e:
        print("An exception occured while sending:")
        print(e)
        send_reply(bot, update, "⚠ *Something getting wrong* while sending payment 💩", reply_to=reply_to)
        raise e
        return


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
    global rta_thread, last_stats
    rta_thread = threading.Thread(target=rta_updater)
    rta_thread.start()
    while True:
        if last_stats:
            print("Initial RTA stats fetched")
            return
        time.sleep(0.25)


def stop_rta_thread(signum, frame):
    global time_to_die, rta_thread
    time_to_die = True
    rta_thread.join()


def error(bot, update, error):
    """Log Errors caused by Updates."""
    logger.warning('Update "%s" caused error "%s"', update, error)


def main():
    print("Starting bot")
    global pp, updater, globaldata

    globaldata = shelve.open(PERSISTENCE_GLOBAL_FILENAME, writeback=True)

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
    globaldata.close()


if __name__ == '__main__':
    main()
