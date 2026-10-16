"""Source-backed regional cohorts from the saved full LiquipediaDB response."""
REGIONS = {'EU': ('europe', 'EUROPE', 18), 'AM': ('americas', 'AMERICAS', 9),
           'AS': ('asia', 'ASIA', 5)}
NAMES = {
    'team spirit 2022': 'Spirit', 'team vitality 2023': 'Vitality', 'mouz 2021': 'MOUZ',
    'g2 esports 2019': 'G2', 'team falcons 2022': 'Falcons', 'fut esports': 'FUT',
    'aurora gaming 2025': 'Aurora', 'astralis 2020': 'Astralis', 'betboom team 2024': 'BETBOOM',
    'b8': 'B8', 'nemiga gaming 2021': 'Nemiga', 'heroic sep 2024': 'HEROIC',
    'luminosity gaming 2018': 'Luminosity', 'monte 2022': 'Monte',
    'ninjas in pyjamas 2021': 'Ninjas in Pyjamas', 'ic esports 2026': 'Inner Circle',
    '3dmax 2024': '3DMAX', 'magic': 'magic', 'alliance 2024': 'Alliance',
    'faze clan 2026': 'FaZe', 'big 2020': 'BIG', 'gamerlegion 2026': 'GamerLegion',
    'natus vincere 2021': 'Natus Vincere', 'parivision': 'PARIVISION',
    'legacy 2023': 'Legacy', 'furia 2025': 'FURIA', 'mibr': 'MIBR',
    '9z team 2024': '9z', 'team liquid 2024': 'Liquid', 'm80 orig': 'M80',
    'dendele cs 2026': 'DENDELE', 'sharks esports 2026': 'Sharks', 'nrg 2024': 'NRG',
    'imperial esports 2025': 'Imperial', 'pain gaming 2023': 'paiN',
    'fluxo w7m': 'Fluxo', 'wildcard 2024': 'Wildcard',
    'tyloo 2016': 'TYLOO', 'bc.game esports nov 2025': 'BC.Game',
    'the mongolz 2024 03': 'The MongolZ', 'lynn vision gaming 2024': 'Lynn Vision',
    '5star esports orig': '5star', 'flyquest 2021': 'FlyQuest',
    'chinggis warriors': 'Chinggis Warriors', 'depo 2025': 'DEPO',
    'thunderdownunder 2026': 'THUNDER dOWNUNDER',
}
CORE_ALIASES = {'Monte': 'Luminosity', 'Sharks': 'DENDELE'}
EXTRA_PALETTE = {
    'Nemiga':'#fc765b', 'HEROIC':'#93cfb5', 'Luminosity':'#64b1e4',
    'Ninjas in Pyjamas':'#e8e585', 'Inner Circle':'#c89ff1', '3DMAX':'#ed806b',
    'magic':'#cfccf3', 'Alliance':'#e6a168', 'BIG':'#d2d8df', 'GamerLegion':'#81caba',
    'Liquid':'#cbd8ec', 'M80':'#b8dae0', 'DENDELE':'#e59b66', 'NRG':'#acb8ed',
    'Imperial':'#8fcd93', 'paiN':'#e18f92', 'Fluxo':'#b0d97c', 'Wildcard':'#e4ba6f',
    'TYLOO':'#e56b5d', 'BC.Game':'#96d34c', 'The MongolZ':'#dab260',
    'Lynn Vision':'#8fccef', '5star':'#dc9eed', 'FlyQuest':'#b3cb67',
    'Chinggis Warriors':'#83c8b6', 'DEPO':'#bbc7d7', 'THUNDER dOWNUNDER':'#d39183',
}


def players(opponent):
    source = opponent.get('players', {})
    source = source.values() if isinstance(source, dict) else source
    return [p['displayName'] for p in source if p.get('displayName')]


def cohorts(raw, palette, same_core):
    records = sorted(raw['response']['result'], key=lambda s:s['date'])
    results = []
    for code, (slug, title, quota) in REGIONS.items():
        complete = []
        for snapshot in records:
            rows = sorted((r for r in snapshot['extradata'].values()
                           if r['opponent'].get('extradata', {}).get('region') == code),
                          key=lambda r:r['place'])
            complete.append([(index+1, r) for index,r in enumerate(rows)])
        selected = {r['opponent']['template'] for rows in complete for _,r in rows[:quota]}
        selected.update(r['opponent']['template'] for _,r in complete[-1][quota:quota+2])
        unknown = selected - NAMES.keys()
        if unknown:
            raise ValueError(f'Unreviewed template identities: {sorted(unknown)}')
        # Verified five-player branding transitions, not approximate name aliases.
        for old, new in CORE_ALIASES.items():
            before, after = [], []
            for rows in complete:
                for _,r in rows:
                    display = NAMES.get(r['opponent']['template'])
                    if display == old: before.append({'roster':players(r['opponent'])})
                    if display == new: after.append({'roster':players(r['opponent'])})
            if before and after and not same_core(before[-1],after[0]):
                raise ValueError(f'Unverified branding bridge: {old} -> {new}')
        names = sorted({CORE_ALIASES.get(NAMES[t], NAMES[t]) for t in selected})
        snapshots, duplicate_choices = [], []
        for snapshot, rows in zip(records,complete):
            best = {}
            for regional_rank,source in rows:
                template = source['opponent']['template']
                if template not in selected: continue
                display = NAMES[template]
                name = CORE_ALIASES.get(display,display)
                if name in best:
                    duplicate_choices.append({'date':snapshot['date'][:10], 'team':name,
                                              'selected_global_rank':best[name]['globalRank'],
                                              'other_global_rank':source['place']})
                    continue
                best[name] = {'name':name, 'displayName':display, 'logoName':display,
                              'rank':regional_rank, 'globalRank':source['place'],
                              'points':source['points'], 'roster':players(source['opponent']),
                              'region':code, 'template':template}
            snapshots.append({'date':snapshot['date'][:10], 'rows':list(best.values())})
        current = {r['name']:r['rank'] for r in snapshots[-1]['rows']}
        names.sort(key=lambda name:current.get(name,9999))
        results.append({'regionCode':code,'regionSlug':slug,'regionLabel':title,'quota':quota,
                        'names':names,'snapshots':snapshots,'palette':{**palette,**EXTRA_PALETTE},
                        'duplicateChoices':duplicate_choices,
                        'brandBridges':CORE_ALIASES,
                        'selection':'Union of regional top-quota at all saved dates, plus two nearest teams after the quota at the latest date',
                        'rankDerivation':'Sort all saved global rows of this source region by their original global rank; number before deduplication',
                        'quotaSource':'https://www.hltv.org/news/44921/regional-distribution-set-for-pgl-singapore-major',
                        'eligibility':'Rank history only; acceptance, intact roster and regional eligibility are not asserted'})
    return results
