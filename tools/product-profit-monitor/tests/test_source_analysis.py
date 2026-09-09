import copy
import pandas as pd
import pytest
from bi_check_agent.source_analysis import analyze, public_evidence, source_frames


def snapshot(n=250):
    rows=pd.DataFrame({'type':['order']*n,'sku':[f'SKU-{i:04d}' for i in range(n)],'store':['A']*n,
        'sales':[10.]*n,'gross_sales':[10.]*n,'promo_cost':[0.]*n,'order_cost_total':[2.]*n,
        'units':[1.]*n,'shipping_fee':[float(i) for i in range(n)],'commission':[0.]*n,'ad_spend':[0.]*n,
        'product_cost_unit':[2.]*n,'order_id':['not-for-ui']*n})
    return {'_source_frames':{'current':rows,'previous':rows.copy()},'current_aggregate':[{'sales':-999}]}


def test_full_source_population_not_top_rows_or_aggregate():
    snap=snapshot()
    result=analyze(snap,{'period':'current','measures':[{'field':'shipping_fee','statistic':'sum'}]})['periods']['current']
    assert result['matched_record_count']==250
    assert result['rows'][0]['shipping_fee__sum']==sum(range(250))
    result=analyze(snap,{'period':'current','group_by':['sku'],'measures':[{'field':'shipping_fee','statistic':'sum'}],'sort_by':'shipping_fee__sum','limit':5})['periods']['current']
    assert result['result_count']==250 and result['returned_count']==5 and result['truncated']
    assert result['rows'][0]['sku']=='SKU-0249'


def test_filtered_statistics_and_source_unchanged():
    snap=snapshot()
    before=snap['_source_frames']['current'].copy(deep=True)
    result=analyze(snap,{'period':'current','filters':[{'field':'shipping_fee','operator':'gte','value':200}], 'measures':[{'field':'shipping_fee','statistic':'sum'}]})['periods']['current']
    assert result['matched_record_count']==50 and result['rows'][0]['shipping_fee__sum']==sum(range(200,250))
    pd.testing.assert_frame_equal(before,snap['_source_frames']['current'])


def test_samples_are_source_rows_and_not_exported_in_evidence():
    result=analyze(snapshot(),{'period':'current','operation':'sample','sample_columns':['sku','shipping_fee'],'limit':100})
    current=result['periods']['current']
    assert current['returned_count']==30 and current['truncated']
    assert 'source_record_ref' in current['rows'][0]
    assert 'rows' not in public_evidence({'source_analyses':[result]})['source_analyses'][0]['periods']['current']
    assert 'rows' in result['periods']['current']


def test_missing_is_not_zero_and_unit_cost_cannot_be_summed():
    snap=snapshot(3)
    snap['_source_frames']['current'].loc[0,'shipping_fee']=None
    current=analyze(snap,{'period':'current','measures':[{'field':'shipping_fee','statistic':'sum'}]})['periods']['current']
    assert current['rows'][0]['shipping_fee__sum'] is None
    assert current['rows'][0]['shipping_fee__missing_count']==1
    with pytest.raises(ValueError,match='不能累加'):
        analyze(snap,{'measures':[{'field':'product_cost_unit','statistic':'sum'}]})


@pytest.mark.parametrize('params',[{'group_by':['order_id']},{'filters':[{'field':'__import__','operator':'eq','value':'os'}]},{'operation':'exec'},{'sql':'select * from x'},{'limit':10000}])
def test_arbitrary_code_and_unsupported_fields_rejected(params):
    with pytest.raises(ValueError):analyze(snapshot(),params)


def test_old_snapshot_requires_reanalysis():
    with pytest.raises(ValueError,match='重新点击'):source_frames({'current_aggregate':[]})


def test_period_comparison_ranks_changes_from_full_source_population():
    snap=snapshot()
    snap['_source_frames']['current'].loc[1,'shipping_fee']=999.
    result=analyze(snap,{'operation':'compare','group_by':['sku'],'measures':[{'field':'shipping_fee','statistic':'sum'}],'limit':3})
    assert result['comparison']['result_count']==250
    assert result['comparison']['rows'][0]['sku']=='SKU-0001'
    assert result['comparison']['rows'][0]['shipping_fee__sum__change']==998.
    assert result['comparison']['truncated']


def test_absent_group_is_not_assumed_zero():
    snap=snapshot(3)
    snap['_source_frames']['current']=snap['_source_frames']['current'].iloc[:2]
    result=analyze(snap,{'operation':'compare','group_by':['sku'],'measures':[{'field':'shipping_fee','statistic':'sum'}]})
    absent=next(r for r in result['comparison']['rows'] if r['sku']=='SKU-0002')
    assert absent['presence']=='left_only'
    assert absent['shipping_fee__sum__change'] is None
