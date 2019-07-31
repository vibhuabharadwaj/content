from WordTokenizeTest import main
from CommonServerPython import formats
import demistomock as demisto


def test_main(mocker):
    mocker.patch.object(demisto, 'args', return_value={'value': '<pre>this is a test</pre>'})
    mocker.patch.object(demisto, 'results')
    main()
    assert demisto.results.call_count == 1
    # call_args is tuple (args list, kwargs). we only need the first one
    results = demisto.results.call_args[0]
    assert len(results) == 1
    assert results[0]['ContentsFormat'] == formats['text']
    assert results[0]['Contents'] == 'this is a test'
