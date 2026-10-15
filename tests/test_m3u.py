from custom_components.stalker.m3u import group_series, parse_m3u

SERIES = '''#EXTM3U
#EXTINF:-1 tvg-logo="http://h/l.jpg" portal-stream-id="a" tvg-type="series" serie-title="Bleach" tvg-season="1" tvg-episode="2" group-title="DE/S ANIME",Bleach S01E02
http://h/proxy/e/a2.mp4
#EXTINF:-1 tvg-logo="http://h/l.jpg" portal-stream-id="b" tvg-type="series" serie-title="Bleach" tvg-season="1" tvg-episode="1" group-title="DE/S ANIME",Bleach S01E01
http://h/proxy/e/a1.mp4
#EXTINF:-1 portal-stream-id="c" group-title="X",Fallback Show S02E10
http://h/proxy/e/c.mp4
#EXTINF:-1,Broken entry without url
#EXTINF:-1,Movie, with comma - 2003
http://h/proxy/e/m
'''


def test_parse_attributes_and_comma_titles():
    e = parse_m3u(SERIES)
    assert [x.title for x in e] == ["Bleach S01E02", "Bleach S01E01", "Fallback Show S02E10",
                                   "Movie, with comma - 2003"]
    assert e[0].logo == "http://h/l.jpg" and e[0].group == "DE/S ANIME" and e[0].stream_id == "a"


def test_non_http_urls_are_dropped():
    assert parse_m3u("#EXTINF:-1,x\nfile:///etc/passwd\n#EXTINF:-1,y\nhttp://ok/1\n")[0].title == "y"


def test_group_series_sorts_and_falls_back_to_title():
    s = group_series(parse_m3u(SERIES))
    assert [ep.episode for ep in s["Bleach"].seasons[1]] == [1, 2]
    assert s["Fallback Show"].seasons[2][0].episode == 10
    assert "Movie, with comma - 2003" not in s  # no SxxExx -> not a series
