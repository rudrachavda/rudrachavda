import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib
from collections import Counter

# Your birthday as YYYY-MM-DD, read from the BIRTHDAY environment variable so it isn't committed to a public repo
BIRTHDAY = os.environ.get('BIRTHDAY')

# ---------- settings you may want to change ----------
LOC_DAYS = 365               # the "Lines" stat only counts commits made in the last this-many days
MAX_LINES_PER_COMMIT = 5000  # ...and ignores any single commit bigger than this (vendored code, lock files, generated files). None = no limit
LINE_WIDTH = 58              # every line of the info column is padded to exactly this many characters. 58 characters end about 38px inside the 985px wide image
SEP_COL = 34                 # column of the "|" on the Repos and Commits lines, so the two lines line up

# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
# Issues and pull requests permissions not needed at the moment, but may be used in the future
HEADERS = {'authorization': 'token '+ os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME'] # your own GitHub username
REPO_EDGES = [] # the repositories found by loc_query(), kept for recent_loc()
WARNINGS = [] # short notes printed at the end of the run (never repository names, so public logs stay private)
QUERY_COUNT = {'verify_token_owner': 0, 'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}


def daily_readme(birthday):
    """
    Returns the length of time since I was born
    e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years), 
        diff.months, 'month' + format_plural(diff.months), 
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    """
    Returns a properly formatted number
    e.g.
    'day' + format_plural(diff.days) == 5
    >>> '5 days'
    'day' + format_plural(diff.days) == 1
    >>> '1 day'
    """
    return 's' if unit != 1 else ''


def post_with_retry(query, variables, tries=6, delay=5):
    """
    Sends a GraphQL request, retrying with a growing delay when GitHub returns a temporary error (502/503/504)
    or the connection drops. Any other response is returned as-is for the caller to handle.
    """
    for attempt in range(tries):
        last_try = attempt == tries - 1
        try:
            request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables':variables}, headers=HEADERS, timeout=120)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if last_try: raise
        else:
            if request.status_code not in (502, 503, 504) or last_try:
                return request
        time.sleep(delay * 2 ** attempt) # with the defaults: 5s, 10s, 20s, 40s, 80s


def simple_request(func_name, query, variables):
    """
    Returns a request, or raises an Exception if the response does not succeed.
    """
    request = post_with_retry(query, variables)
    if request.status_code == 200:
        return request
    raise Exception(func_name, ' has failed with a', request.status_code, request.text, QUERY_COUNT)


def graph_commits(start_date, end_date):
    """
    Uses GitHub's GraphQL v4 API to return my total commit count
    """
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date,'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def graph_repos_stars(count_type, owner_affiliation, cursor=None, add_loc=0, del_loc=0):
    """
    Uses GitHub's GraphQL v4 API to return my total repository, star, or lines of code count.
    """
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if count_type == 'stars':
        edges = request.json()['data']['user']['repositories']['edges']
        missing = sum(1 for edge in edges if not edge or not edge['node'])
        if missing:
            error_types = sorted({str(error.get('type')) for error in request.json().get('errors', [])})
            WARNINGS.append(str(missing) + ' repositories returned no data for the star count and were skipped (' + ', '.join(error_types or ['no reason given']) + ')')
    if request.status_code == 200:
        if count_type == 'repos':
            return request.json()['data']['user']['repositories']['totalCount']
        elif count_type == 'stars':
            return stars_counter(request.json()['data']['user']['repositories']['edges'])


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None, page_size=100, since=None):
    """
    Uses GitHub's GraphQL v4 API and cursor pagination to fetch 100 commits from a repository at a time
    """
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String, $page_size: Int!, $author_id: ID!SINCE_DECLARATION) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: $page_size, after: $cursor, author: {id: $author_id}SINCE_ARGUMENT) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    query = query.replace('SINCE_DECLARATION', ', $since: GitTimestamp!' if since else '').replace('SINCE_ARGUMENT', ', since: $since' if since else '')
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor, 'page_size': page_size, 'author_id': OWNER_ID['id']}
    if since: variables['since'] = since
    request = post_with_retry(query, variables, tries=2, delay=3) # I cannot use simple_request(), because I want to save the file before raising Exception
    if request.status_code in (502, 503, 504):
        # GitHub times out when asked to total up too many (or too large) commits at once.
        # Ask again for fewer commits from the same spot; if even 1 commit times out, skip this repository for now.
        if page_size > 1:
            return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, cursor, max(1, page_size // 4), since)
        WARNINGS.append('a repository was skipped because GitHub kept timing out on it; it will be retried on the next run')
        return 0 # cache_builder records this as "0 0 0 0", which makes the next run try this repository again
    if request.status_code == 200:
        if request.json()['data']['repository']['defaultBranchRef'] != None: # Only count commits if repo isn't empty
            return loc_counter_one_repo(owner, repo_name, data, cache_comment, request.json()['data']['repository']['defaultBranchRef']['target']['history'], addition_total, deletion_total, my_commits, page_size, since)
        else: return 0
    force_close_file(data, cache_comment) # saves what is currently in the file before this program crashes
    if request.status_code == 403:
        raise Exception('Too many requests in a short amount of time!\nYou\'ve hit the non-documented anti-abuse limit!')
    raise Exception('recursive_loc() has failed with a', request.status_code, request.text, QUERY_COUNT)


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits, page_size=100, since=None):
    """
    Recursively call recursive_loc (since GraphQL can only search 100 commits at a time) 
    only adds the LOC value of commits authored by me
    """
    for node in history['edges']:
        if node['node']['author']['user'] == OWNER_ID:
            if since and MAX_LINES_PER_COMMIT and node['node']['additions'] + node['node']['deletions'] > MAX_LINES_PER_COMMIT:
                continue # recent lines only: ignore huge commits (vendored code, lock files, generated files)
            my_commits += 1
            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']

    if history['edges'] == [] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    else: return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'], min(100, page_size * 2), since)


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=[]):
    """
    Uses GitHub's GraphQL v4 API to query all the repositories I have access to (with respect to owner_affiliation)
    Queries 60 repos at a time, because larger queries give a 502 timeout error and smaller queries send too many
    requests and also give a 502 error.
    Returns the total number of lines of code in all repositories
    """
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
            edges {
                node {
                    ... on Repository {
                        nameWithOwner
                        defaultBranchRef {
                            target {
                                ... on Commit {
                                    history {
                                        totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    if request.json()['data']['user']['repositories']['pageInfo']['hasNextPage']:   # If repository data has another page
        edges += request.json()['data']['user']['repositories']['edges']            # Add on to the LoC count
        return loc_query(owner_affiliation, comment_size, force_cache, request.json()['data']['user']['repositories']['pageInfo']['endCursor'], edges)
    else:
        REPO_EDGES[:] = [edge for edge in edges + request.json()['data']['user']['repositories']['edges'] if edge and edge['node']]
        return cache_builder(REPO_EDGES, comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Checks each repository in edges to see if it has been updated since the last time it was cached
    If it has, run recursive_loc on that repository to update the LOC count
    """
    cached = True # Assume all repositories are cached
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' # Create a unique filename for each user
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError: # If the cache file doesn't exist, create it
        data = []
        if comment_size > 0:
            for _ in range(comment_size): data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data)-comment_size != len(edges) or force_cache: # If the number of repos has changed, or force_cache is True
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size] # save the comment block
    data = data[comment_size:] # remove those lines
    updated = 0
    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        before = data[index]
        if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                if int(commit_count) != edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']:
                    # if commit count has changed, update loc for that repo
                    owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = repo_hash + ' ' + str(edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']) + ' ' + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
            except TypeError: # If the repo is empty
                data[index] = repo_hash + ' 0 0 0 0\n'
        if data[index] != before: # save after every finished repository, so stopping or crashing never loses finished work
            updated += 1
            with open(filename, 'w') as f:
                f.writelines(cache_comment)
                f.writelines(data)
            print('\r   Reading repository history: ' + str(updated) + ' updated, ' + str(index + 1) + ' of ' + str(len(edges)) + ' checked', end='', flush=True)
    if updated: print('\r' + ' ' * 70 + '\r', end='', flush=True)
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def recent_loc(comment_size):
    """
    Lines added and deleted in the commits I made during the last LOC_DAYS days (huge commits ignored, see MAX_LINES_PER_COMMIT).
    Uses the repository list from loc_query() and its cache, which says which repositories I have commits in.
    This is one quick request per such repository (only my recent commits are fetched), so it is not cached.
    """
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=LOC_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt'
    with open(filename, 'r') as f:
        lines = f.readlines()
    cache_comment, data = lines[:comment_size], lines[comment_size:]
    my_commits = {line.split()[0]: int(line.split()[2]) for line in data}
    added = deleted = 0
    for edge in REPO_EDGES:
        if my_commits.get(hashlib.sha256(edge['node']['nameWithOwner'].encode('utf-8')).hexdigest(), 0) == 0:
            continue # I have no commits in this repository
        owner, repo_name = edge['node']['nameWithOwner'].split('/')
        result = recursive_loc(owner, repo_name, data, cache_comment, since=since)
        if result: # 0 means the repository is empty or was skipped
            added += result[0]
            deleted += result[1]
    return [added, deleted, added - deleted]


def flush_cache(edges, filename, comment_size):
    """
    Wipes the cache file
    This is called when the number of repositories changes or when the file is first created
    """
    with open(filename, 'r') as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size] # only save the comment
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def force_close_file(data, cache_comment):
    """
    Forces the file to close, preserving whatever data was written to it
    This is needed because if this function is called, the program would've crashed before the file is properly saved and closed
    """
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print('There was an error while writing to the cache file. The file,', filename, 'has had the partial data saved and closed.')


def stars_counter(data):
    """
    Count total stars in repositories owned by me
    """
    total_stars = 0
    for node in data:
        if node and node['node']: total_stars += node['node']['stargazers']['totalCount'] # skip repositories GitHub returned as null
    return total_stars


SVG_NS = '{http://www.w3.org/2000/svg}'
MIN_LEADER = 3 # shortest dot leader that still looks like one: " . "


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    """
    Parse SVG files and update elements with my age, commits, stars, repositories, and lines written.
    Every line of the info column ends up exactly LINE_WIDTH characters wide, so nothing can run past the right-hand padding.
    """
    tree = etree.parse(filename)
    root = tree.getroot()
    numbers = {'commit_data': commit_data, 'star_data': star_data, 'repo_data': repo_data, 'contrib_data': contrib_data,
               'follower_data': follower_data, 'loc_data': loc_data[2], 'loc_add': loc_data[0], 'loc_del': loc_data[1]}
    find_and_replace(root, 'age_data', age_data)
    for shorten in (format_number, abbreviate): # full numbers first; abbreviated ones (1.2M) only if the full ones do not fit
        for element_id, value in numbers.items():
            find_and_replace(root, element_id, shorten(value))
        too_long = fit_info_lines(root)
        if not too_long: break
    if too_long:
        raise Exception(filename + ' has lines wider than ' + str(LINE_WIDTH) + ' characters even with abbreviated numbers (y=' + ', y='.join(too_long) + '). Shorten that text in the SVG.')
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def format_number(value):
    """1234567 -> '1,234,567'"""
    return '{:,}'.format(value) if isinstance(value, int) else str(value)


def abbreviate(value):
    """Short form, used only when the full numbers do not fit on a line: 1234567 -> '1.2M'"""
    if abs(value) < 1000: return str(value)
    for size, suffix in ((1000, 'k'), (1000000, 'M'), (1000000000, 'B')):
        if abs(value) / size < 999.95 or suffix == 'B': # 999,999 becomes 1M, not 1000k
            return ('{:.1f}'.format(value / size)).rstrip('0').rstrip('.') + suffix


def char_width(char):
    """Emoji, like the birthday cake, are drawn two characters wide"""
    return 2 if ord(char) >= 0x1F300 else 1


def info_lines(root):
    """
    Splits the info column (the <text> that starts at x=390) into lines.
    A line starts at an element with a y attribute and includes every element after it, up to the next line.
    """
    column = [text for text in root.iter(SVG_NS + 'text') if text.get('x') == '390'][0]
    lines = []
    for node in column:
        if node.get('y') is not None: lines.append([])
        lines[-1].append(node)
    return lines


def line_text(line):
    return ''.join(''.join(node.itertext()) + (node.tail or '') for node in line).rstrip('\n')


def line_length(line):
    return sum(char_width(char) for char in line_text(line))


def leaders(length):
    """A dot leader that is exactly `length` characters long"""
    if length <= 0: return ''
    if length == 1: return ' '
    if length == 2: return '. '
    return ' ' + '.' * (length - 2) + ' '


def fit_info_lines(root):
    """
    Pads every line to exactly LINE_WIDTH characters by stretching its dot leaders (the elements whose id ends in _dots).
    Returns the y of every line that is still too wide. Lines with statistics can be fixed by abbreviating their numbers;
    any other line has to be shortened in the SVG itself.
    """
    too_long = []
    for line in info_lines(root):
        stretch = [node for node in line if (node.get('id') or '').endswith('_dots') and node.get('id') != 'loc_del_dots']
        for node in line:
            if node.get('id') == 'loc_del_dots': node.text = ''
        for node in stretch: node.text = ''
        room = LINE_WIDTH - line_length(line) # characters left over for the dot leaders
        if room < 0:
            too_long.append(line[0].get('y'))
            continue
        if not stretch: continue
        if len(stretch) == 1:
            sizes = [room]
        else: # Repos and Commits lines: two leaders, with the "|" kept in column SEP_COL
            bar = line_text(line).find('|')
            first = room // 2 if bar < 0 else SEP_COL - bar
            first = max(MIN_LEADER, min(first, room - MIN_LEADER)) if room >= 2 * MIN_LEADER else room // 2
            sizes = [first, room - first]
        for node, size in zip(stretch, sizes): node.text = leaders(size)
    return too_long


def find_and_replace(root, element_id, new_text):
    """
    Finds the element in the SVG file and replaces its text with a new value
    """
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def commit_counter(comment_size):
    """
    Counts up my total commits, using the cache file created by cache_builder.
    """
    total_commits = 0
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' # Use the same filename as cache_builder
    with open(filename, 'r') as f:
        data = f.readlines()
    cache_comment = data[:comment_size] # save the comment block
    data = data[comment_size:] # remove those lines
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits


def verify_token_owner():
    """
    Stops the script unless ACCESS_TOKEN belongs to USER_NAME.
    Prevents the stats from silently coming from another account (e.g. a username left over from a copied repo).
    """
    query_count('verify_token_owner')
    request = simple_request(verify_token_owner.__name__, 'query { viewer { login } }', {})
    login = request.json()['data']['viewer']['login']
    if login.lower() != USER_NAME.lower():
        raise Exception("ACCESS_TOKEN belongs to '" + login + "' but USER_NAME is '" + USER_NAME + "'. Fix one of them so the stats come from your own profile.")
    return login


def user_getter(username):
    """
    Returns the account ID and creation time of the user
    """
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']

def follower_getter(username):
    """
    Returns the number of followers of the user
    """
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    """
    Counts how many times the GitHub GraphQL API is called
    """
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """
    Calculates the time it takes for a function to run
    Returns the function result and the time differential
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    """
    Prints a formatted time differential
    Returns formatted result if whitespace is specified, otherwise returns raw result
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == '__main__':
    """
    Andrew Grant (Andrew6rant), 2022-2025
    """
    if not BIRTHDAY:
        raise SystemExit('Set the BIRTHDAY environment variable (YYYY-MM-DD) first.')
    birthday = datetime.datetime.strptime(BIRTHDAY, '%Y-%m-%d')
    os.makedirs('cache', exist_ok=True)
    print('Stats for GitHub user:', verify_token_owner())
    print('Calculation times:')
    # define global variable for owner ID and calculate user's creation date
    # e.g {'id': 'MDQ6VXNlcjEyMzQ1'} and 2019-11-03T21:15:07Z (example format only)
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter('account data', user_time)
    age_data, age_time = perf_counter(daily_readme, birthday)
    formatter('age calculation', age_time)
    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    formatter('LOC (cached)', loc_time) if total_loc[-1] else formatter('LOC (no cache)', loc_time)
    recent_data, recent_time = perf_counter(recent_loc, 7)
    formatter('LOC (last ' + str(LOC_DAYS) + ' days)', recent_time)
    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, recent_data)
    svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, recent_data)

    print('{:<23}'.format('   Total function time:'), '{:>12}'.format('%.4f' % (user_time + age_time + loc_time + recent_time + commit_time + star_time + repo_time + contrib_time) + ' s '), sep='')

    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items(): print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))
    for warning, count in Counter(WARNINGS).items(): print('Note:', warning, '(x' + str(count) + ')')