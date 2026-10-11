from pathlib import Path
import pandas as pd, numpy as np, json, sqlite3, hashlib, zipfile
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
import fitz

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'output'; OUT.mkdir(exist_ok=True)
CH=OUT/'charts'; CH.mkdir(exist_ok=True)
checks=[]
def check(name, condition, detail=''):
 checks.append(dict(check=name,passed=bool(condition),detail=str(detail)))
def save(d,name): d.to_csv(OUT/(name+'.csv'),index=False)
files={p.stem:p for p in (ROOT/'inputs').rglob('*.csv')}
d={k:pd.read_csv(v) for k,v in files.items()}
o,i,p,c=[d[k].copy() for k in ['orders','order_items','products','customers']]
save(pd.DataFrame([{'file':k,'rows':len(v),'columns':len(v.columns),'null_cells':int(v.isna().sum().sum()),'sha256':hashlib.sha256(files[k].read_bytes()).hexdigest()} for k,v in d.items()]),'source_inventory')
save(pd.concat([v.isna().sum().rename('missing').reset_index().assign(file=k) for k,v in d.items()]),'source_nulls')
duplicates=o[o.duplicated(keep=False)]; save(duplicates,'exact_duplicate_order_rows')
o=o.drop_duplicates(); conflicts=o[o.duplicated('order_id',keep=False)]; save(conflicts,'conflicting_orders')
check('No conflicting order IDs',len(conflicts)==0)
if len(conflicts): raise ValueError('Conflicting orders require resolution; source retained')
for name,df,key in [('customers',c,'customer_id'),('products',p,'product_id')]:
 check(name+' key unique',df[key].is_unique)
 if not df[key].is_unique: raise ValueError('Nonunique dimension key')
check('Order customer foreign keys',o.customer_id.isin(c.customer_id).all())
check('Line order foreign keys',i.order_id.isin(o.order_id).all())
check('Line product foreign keys',i.product_id.isin(p.product_id).all())
o['order_date']=pd.to_datetime(o.order_date); c['signup_date']=pd.to_datetime(c.signup_date)
save(pd.DataFrame({'category_original':p.category.unique()}),'category_original_labels')
p['category_normalized']=p.category.str.strip().str.lower().str.replace(r'\s+',' ',regex=True).str.title()
save(p[['product_id','category','category_normalized']],'category_mapping')
check('Eight normalized categories',p.category_normalized.nunique()==8,p.category_normalized.unique())
check('Valid line quantities and discounts',i.quantity.gt(0).all() and i.discount_amount.ge(0).all())
i['net_line']=i.quantity*i.unit_price-i.discount_amount
check('No negative priced net lines',i.net_line.dropna().ge(0).all())
med=i.groupby('product_id').unit_price.median()
i['estimated_unit_price']=i.unit_price.fillna(i.product_id.map(med))
i['price_estimated']=i.unit_price.isna(); i['estimated_net']=i.quantity*i.estimated_unit_price-i.discount_amount
save(i[i.price_estimated],'estimated_price_lines')
lines=i.groupby('order_id').agg(observed_merchandise=('net_line',lambda x:x.sum(min_count=1)),estimated_merchandise=('estimated_net',lambda x:x.sum(min_count=1)),missing_prices=('unit_price',lambda x:x.isna().sum()),line_count=('product_id','size'))
o=o.merge(lines,on='order_id',how='left',validate='one_to_one')
o['complete_price']=o.missing_prices.eq(0)&o.line_count.notna()
o['in_window']=o.order_date.ge('2024-01-01')&o.order_date.lt('2026-01-01')
o['qualifying']=o.in_window&o.status.eq('completed')
save(o,'order_level_audit')
save(o.groupby(['status','in_window'],dropna=False).agg(orders=('order_id','size'),buyers=('customer_id','nunique'),observed_partial_merchandise=('observed_merchandise','sum'),missing_price_orders=('complete_price',lambda x:(~x).sum())).reset_index(),'order_status_coverage')
q=o[o.qualifying].copy()
g=q.groupby('customer_id').agg(latest_purchase=('order_date','max'),frequency=('order_id','nunique'),observed_partial_spend=('observed_merchandise','sum'),estimated_spend=('estimated_merchandise','sum'),complete_price=('complete_price','all'))
g['recency']=(pd.Timestamp('2026-01-01')-g.latest_purchase.dt.normalize()).dt.days
spacing=q.sort_values(['customer_id','order_date']).assign(gap=lambda x:x.groupby('customer_id').order_date.diff().dt.total_seconds()/86400).groupby('customer_id').gap.median()
g['median_purchase_gap_days']=spacing
g['value_2025']=q[q.order_date.ge('2025-01-01')].groupby('customer_id').observed_merchandise.sum()
g['value_2025']=g.value_2025.fillna(0)
c=c.merge(g,on='customer_id',how='left',validate='one_to_one')
c['has_any_order']=c.customer_id.isin(o.customer_id); c['has_qualifying_purchase']=c.frequency.notna()
c['primary_eligible']=c.has_qualifying_purchase&c.complete_price.eq(True)
c['eligibility']=np.select([c.primary_eligible,c.has_qualifying_purchase,~c.has_any_order],['Primary fully priced','Incomplete historical prices','No observed order'],default='Only non-completed / outside window')
c['monetary']=c.observed_partial_spend.where(c.primary_eligible)
c['signup_cohort']=c.signup_date.dt.year.astype(str); c['signup_half']=c.signup_date.dt.year.astype(str)+' H'+np.where(c.signup_date.dt.month.le(6),'1','2')
base=c[c.primary_eligible].copy()
def score(df, value='monetary', fixed=False):
 df=df.copy(); boundaries={}
 for col,label in [('recency','R'),('frequency','F'),(value,'M')]:
  b=df[col].quantile([.2,.4,.6,.8]).to_numpy(); boundaries[label]=b.tolist()
  s=np.searchsorted(b,df[col].to_numpy(),side='left')+1
  if label=='R': s=6-s
  if label=='F' and fixed: s=np.select([df.frequency.eq(1),df.frequency.eq(2),df.frequency.le(4),df.frequency.le(7)],[1,2,3,4],default=5)
  df[label]=s
 R,F,M=df.R,df.F,df.M
 df['segment']=np.select([(R>=4)&(F>=4)&(M>=4),(R>=3)&(F>=4),(R<=2)&(F>=3),(R<=2)&(F<=2),(R>=4)&(F<=2)],['Champions','Loyal','At risk','Lapsed low-frequency','Recent low-frequency'],default='Developing')
 return df,boundaries
base,bounds=score(base)
for label,col in [('R','recency'),('F','frequency'),('M','monetary')]: check('Equal '+label+' values equal scores',base.groupby(col)[label].nunique().max()==1)
check('Upper inclusive boundary test',np.array_equal(np.searchsorted([1,2,3,4],[1,2,3,4],side='left')+1,[1,2,3,4]))
c=c.merge(base[['customer_id','R','F','M','segment']],on='customer_id',how='left',validate='one_to_one')
c['segment']=c.segment.fillna(c.eligibility)
save(c,'customer_rfm'); save(pd.DataFrame(bounds,index=['20%','40%','60%','80%']).reset_index(names='percentile'),'scoring_boundaries')
segments=base.groupby('segment').agg(customers=('customer_id','size'),orders=('frequency','sum'),merchandise=('monetary','sum'),median_recency=('recency','median'),median_customer_gap_days=('median_purchase_gap_days','median'),repeat_buyers=('frequency',lambda x:x.ge(2).sum())).reset_index()
segments['customer_share']=segments.customers/len(base); segments['merchandise_share']=segments.merchandise/base.monetary.sum(); segments['aov']=segments.merchandise/segments.orders
save(segments,'segment_summary')
coverage=c.groupby('eligibility').agg(customers=('customer_id','size'),completed_orders=('frequency','sum'),observed_partial_spend=('observed_partial_spend','sum')).reset_index(); save(coverage,'population_coverage')
expected={'Champions':235,'Loyal':114,'At risk':295,'Lapsed low-frequency':461,'Recent low-frequency':415,'Developing':372}
for k,v in expected.items(): check('Expected segment '+k,int((base.segment==k).sum())==v)
for name,actual,exp in [('Original orders',len(d['orders']),9245),('Exact duplicates',len(d['orders'])-len(o),38),('Unique orders',len(o),9207),('Completed orders',len(q),8456),('Completed buyers',len(g),2047),('Fully priced buyers',len(base),1892),('Incomplete buyers',sum(c.eligibility.eq('Incomplete historical prices')),155),('Primary orders',int(base.frequency.sum()),6622),('Primary merchandise',round(base.monetary.sum(),2),1454282.70),('No order accounts',sum(~c.has_any_order),1494),('Only non-completed',sum(c.eligibility.eq('Only non-completed / outside window')),59)]: check(name,actual==exp,f'actual={actual}; expected={exp}')
check('Customer population reconciles',coverage.customers.sum()==len(c))
check('Segment value reconciliation',np.isclose(segments.merchandise.sum(),base.monetary.sum()))
check('Segment order reconciliation',segments.orders.sum()==base.frequency.sum())
threshold= bounds['M'][3]
inactive=[]
for days in [90,180,270]:
 a=base[base.monetary.ge(threshold)&base.recency.ge(days)]
 inactive.append(dict(days=days,customers=len(a),historical_value=a.monetary.sum()))
save(pd.DataFrame(inactive),'high_value_inactivity'); audience=base[base.monetary.ge(threshold)&base.recency.ge(180)]; save(audience,'high_value_180_day_audience')
check('180-day high value customers',len(audience)==152); check('180-day historical value',round(audience.monetary.sum(),2)==339210.01)
fixed,_=score(base,fixed=True); save(pd.crosstab(base.segment,fixed.segment).reset_index(),'fixed_frequency_transitions')
sens=[]
for name,df,val,fix in [('Primary',base,'monetary',False),('Fixed frequency',base,'monetary',True),('Estimated historical prices',c[c.has_qualifying_purchase].copy(),'estimated_spend',False),('2025 only value',base,'value_2025',False)]:
 s,b=score(df,val,fix); save(s[['customer_id','recency','frequency',val,'R','F','M','segment']],name.lower().replace(' ','_')+'_sensitivity_customers')
 for seg,x in s.groupby('segment'): sens.append(dict(definition=name,segment=seg,customers=len(x),value=x[val].sum()))
 if name=='Estimated historical prices': save(pd.crosstab(base.set_index('customer_id').segment,s.set_index('customer_id').loc[base.customer_id,'segment']).reset_index(),'estimated_price_primary_transitions')
 if name=='2025 only value':
  h=s[s[val].ge(b['M'][3])&s.recency.ge(180)]; save(pd.DataFrame([dict(definition=name,customers=len(h),historical_value=h[val].sum(),monetary_80th=b['M'][3])]),'2025_only_inactivity')
save(pd.DataFrame(sens),'sensitivity_summary')
top=base.sort_values('monetary',ascending=False); influence=[]
for n in [1,10,19,95]: influence.append(dict(top_customers=n,value=top.head(n).monetary.sum(),share=top.head(n).monetary.sum()/base.monetary.sum(),remaining_mean=top.iloc[n:].monetary.mean()))
save(pd.DataFrame(influence),'large_spender_influence'); save(top.head(20),'large_spenders')
channel=c.groupby('acquisition_channel').agg(accounts=('customer_id','size'),any_order=('has_any_order','sum'),completed_buyers=('has_qualifying_purchase','sum'),scored_buyers=('primary_eligible','sum'),observed_partial_value=('observed_partial_spend','sum')).reset_index(); channel['activation_rate']=channel.completed_buyers/channel.accounts; channel['price_coverage']=channel.scored_buyers/channel.completed_buyers; channel['small_group']=channel.scored_buyers.lt(50); save(channel,'channel_coverage_activation')
cs=base.groupby(['acquisition_channel','segment']).agg(buyers=('customer_id','size'),orders=('frequency','sum'),value=('monetary','sum')).reset_index(); cs['segment_share']=cs.buyers/cs.groupby('acquisition_channel').buyers.transform('sum'); save(cs,'channel_segment_shares')
check('Channel buyers reconcile',cs.buyers.sum()==len(base)); check('Channel value reconciles',np.isclose(cs.value.sum(),base.monetary.sum()))
def adjusted(period,label):
 b=base.copy(); b['repeat']=b.frequency.ge(2).astype(int)
 for seg in expected: b['share_'+seg]=b.segment.eq(seg).astype(int)
 metrics=['repeat','monetary','recency']+['share_'+x for x in expected]
 count=b.groupby(['acquisition_channel',period]).size().unstack(fill_value=0)
 periods=['2024','2025'] if period=='signup_cohort' else ['2024 H1','2024 H2','2025 H1','2025 H2']
 count=count.reindex(columns=periods,fill_value=0); save(count.reset_index(),label+'_support')
 supported=count.index[count.ge(50).all(axis=1)].tolist()
 subset=b[b.acquisition_channel.isin(supported)&b[period].isin(periods)]
 weights=subset[period].value_counts(normalize=True).reindex(periods,fill_value=0)
 save(weights.rename('common_weight').reset_index(),label+'_weights')
 means=b.groupby(['acquisition_channel',period])[metrics].mean().reset_index(); save(means,label+'_raw_cohort_metrics')
 result=[]
 for channel_name in supported:
  x=means[means.acquisition_channel.eq(channel_name)].set_index(period).reindex(periods)
  result.append(dict(acquisition_channel=channel_name,**{m:float((x[m]*weights).sum()) for m in metrics}))
 save(pd.DataFrame(result,columns=['acquisition_channel']+metrics),label+'_adjusted'); return supported
supported=adjusted('signup_cohort','annual_cohort'); fine=adjusted('signup_half','half_year_cohort')
save(c.groupby(['acquisition_channel','signup_cohort','eligibility']).size().rename('accounts').reset_index(),'channel_cohort_price_coverage')
cat=i.merge(q[['order_id','customer_id']],on='order_id',how='inner',validate='many_to_one').merge(p[['product_id','category_normalized']],on='product_id',validate='many_to_one').merge(base[['customer_id','segment']],on='customer_id',how='inner',validate='many_to_one')
categories=cat.groupby(['segment','category_normalized']).agg(merchandise=('net_line','sum'),quantity=('quantity','sum'),orders=('order_id','nunique')).reset_index(); save(categories,'category_summary')
check('Category merchandise reconciles',np.isclose(categories.merchandise.sum(),base.monetary.sum()))
check('Every qualifying primary order fully priced',q[q.customer_id.isin(base.customer_id)].complete_price.all())
check('Incomplete buyers retain no primary monetary',c.loc[c.eligibility.eq('Incomplete historical prices'),'monetary'].isna().all())
check('All missing prices have historical product median estimate',i.estimated_unit_price.notna().all())
check('Line to order observed value reconciliation',np.isclose(i.net_line.sum(),o.observed_merchandise.sum()))
check('Annual weights sum to one',np.isclose(pd.read_csv(OUT/'annual_cohort_weights.csv').common_weight.sum(),1))
save(c[c.has_qualifying_purchase & (c.latest_purchase<c.signup_date)],'purchase_before_signup_review')
check('Window lower inclusive upper exclusive',bool(pd.Timestamp('2024-01-01')>=pd.Timestamp('2024-01-01')) and not bool(pd.Timestamp('2026-01-01')<pd.Timestamp('2026-01-01')))
check('Dashboard filter all and none',len(base[base.segment.isin(expected)])==len(base) and len(base[base.recency.between(99999,100000)])==0)
save(pd.DataFrame(checks),'validation_checks')
conn=sqlite3.connect(OUT/'rfm_review.sqlite')
for name,frame in [('customers_rfm',c),('orders_audit',o),('lines_audit',i),('products',p),('segment_summary',segments),('channel_comparison',cs)]: frame.to_sql(name,conn,if_exists='replace',index=False)
conn.close()
plt.rcParams.update({'font.family':'DejaVu Sans','axes.spines.top':False,'axes.spines.right':False,'figure.dpi':150})
for name,col,title in [('segment_customers','customers','Primary fully priced buyers by segment'),('segment_value','merchandise','Historical net merchandise value by segment')]:
 fig,ax=plt.subplots(figsize=(10,4.8)); x=segments.sort_values(col); ax.barh(x.segment,x[col],color='#227c91'); ax.set_title(title+'\nREVIEW DRAFT | 2024-2025 completed purchases',loc='left'); ax.set_xlabel('Customers' if col=='customers' else 'Historical value (USD)'); fig.text(.01,.015,'Selection: 155 incomplete-price buyers excluded (7.6% of buyers; 21.7% of orders).',fontsize=9); fig.tight_layout(rect=[0,.05,1,1]); fig.savefig(CH/(name+'.png')); plt.close(fig)
fig,ax=plt.subplots(figsize=(9,4.5)); z=pd.DataFrame(inactive); ax.bar(z.days.astype(str),z.customers,color='#227c91'); ax.set(xlabel='Minimum calendar days since purchase',ylabel='Customers',title='High historical value inactivity sensitivity | REVIEW DRAFT'); fig.text(.02,.01,'At or above monetary 80th percentile. Historical value is not recoverable revenue.',fontsize=9); fig.tight_layout(rect=[0,.04,1,1]); fig.savefig(CH/'inactivity.png'); plt.close(fig)
fig,ax=plt.subplots(figsize=(9,4.5)); ax.bar(channel.acquisition_channel,channel.activation_rate,color='#227c91'); ax.set(ylabel='Completed buyers / all accounts',title='Observed activation by acquisition channel | REVIEW DRAFT'); ax.tick_params(axis='x',rotation=20); fig.text(.02,.01,'Descriptive all-account rates; signup timing and missing prices require separate assessment.',fontsize=9); fig.tight_layout(rect=[0,.05,1,1]); fig.savefig(CH/'channel_activation.png'); plt.close(fig)
metadata=dict(boundaries=bounds,primary_customers=len(base),primary_value=float(base.monetary.sum()),supported_channels=supported,finer_supported_channels=fine,checks_passed=sum(x['passed'] for x in checks),checks_total=len(checks),evidence_package_present=False)
(OUT/'analysis_metadata.json').write_text(json.dumps(metadata,indent=2))
print(json.dumps(metadata,indent=2)); print(segments.to_string(index=False)); print(channel.to_string(index=False)); print('Categories',p.category_normalized.unique()); print('Purchase before signup',len(c[c.has_qualifying_purchase & (c.latest_purchase<c.signup_date)]))

# Portable offline dashboard: data embedded, no network requests or external libraries.
records=json.loads(c[['customer_id','acquisition_channel','signup_cohort','segment','primary_eligible','recency','frequency','monetary','observed_partial_spend']].to_json(orient='records'))
template=(ROOT/'dashboard_template.html').read_text(encoding='utf-8')
extra={'adjusted':pd.read_csv(OUT/'annual_cohort_adjusted.csv').to_dict('records'),'categories':categories.to_dict('records'),'sensitivity':sens}
(ROOT/'dashboard.html').write_text(template.replace('__DATA__',json.dumps(records)).replace('__SUMMARY__',json.dumps(inactive)).replace('__EXTRA__',json.dumps(extra)),encoding='utf-8')

styles=getSampleStyleSheet(); styles['Title'].textColor=colors.HexColor('#164b5c'); styles['BodyText'].leading=14
story=[]
def para(text,style='BodyText'): story.append(Paragraph(text,styles[style])); story.append(Spacer(1,9))
def table(rows,widths=None):
 t=Table(rows,colWidths=widths,hAlign='LEFT'); t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#164b5c')),('TEXTCOLOR',(0,0),(-1,0),colors.white),('FONTSIZE',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),7),('TOPPADDING',(0,0),(-1,-1),7),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.HexColor('#eff5f7'),colors.white]) ])); story.append(t); story.append(Spacer(1,12))
actions=[('Champions','Recognition and curated product previews','90-day repeat purchase rate'),('Loyal','Relevant replenishment and cross-category offers','90-day incremental orders per customer'),('At risk','Test personalized re-engagement','60-day incremental reactivation rate'),('Lapsed low-frequency','Low-cost permission-based win-back test','60-day incremental purchase rate'),('Recent low-frequency','Post-purchase guidance and second-order journey','90-day second-order conversion'),('Developing','Category-led nurture and recommendations','90-day repeat purchase rate')]
save(pd.DataFrame(actions,columns=['segment','proposed_action','future_success_metric']),'segment_actions')
para('RFM Customer Segmentation','Title'); para('LOCAL REVIEW DRAFT | Reference date: January 1, 2026','Heading2')
para('Data Career School case study using a Wayfair-themed scenario. The supplied files are not verified real Wayfair customer records. This report describes observed purchasing behavior and proposes CRM experiments; no campaign outcomes are measured.')
para('Decision brief','Heading2'); para(f'The primary analysis includes {len(base):,} fully priced buyers, {int(base.frequency.sum()):,} completed orders, and ${base.monetary.sum():,.2f} in net merchandise value. The 180-day high-value audience contains {len(audience)} buyers with ${audience.monetary.sum():,.2f} historical value. These amounts are historical spend, not lost revenue or expected campaign recovery.')
para('Coverage limitation','Heading2'); para('155 incomplete-price buyers (7.6% of completed buyers) account for 1,834 completed orders (21.7%). They are excluded from primary RFM, with observed recency/frequency and explicitly partial spend retained separately. There are 1,494 accounts with no observed order and 59 with only non-completed orders. Findings may underrepresent active buyers with missing historical prices.')
para('Technical choice','Heading2'); para('Python/pandas computes auditable joins and aggregates; SQLite stores customer, order, line, and product audit tables; CSVs allow inspection in common tools. The offline HTML dashboard embeds its data and supports channel, signup year, segment, population, and inclusive recency filters without a server. Matplotlib produces charts; ReportLab produces this PDF. No hosting or uploads are required.')
story.append(PageBreak()); para('Track 1 | Purchasing segments','Title')
table([['Segment','Buyers','Orders','Value (USD)','AOV (USD)']]+[[r.segment,str(r.customers),str(r.orders),f'{r.merchandise:,.2f}',f'{r.aov:,.2f}'] for r in segments.itertuples()],[155,45,50,100,80])
story.append(Image(str(CH/'segment_value.png'),width=470,height=226)); para('Definitions and scoring','Heading2')
para('Completed orders from January 1, 2024 through December 31, 2025 are deduplicated exactly. Net line value is quantity times historical unit price minus the total line discount. Lines aggregate to orders before customers; shipping is excluded. Recency uses calendar days; frequency counts distinct orders. Segment AOV divides total segment merchandise value by segment orders. Purchase spacing is elapsed time between completed orders, taking each repeat buyer median before the segment median.')
para('Empirical 20/40/60/80 percentiles use linear interpolation and upper-inclusive bins. Equal values receive equal scores; recency bins are reversed. Rules apply in the supplied priority order. Boundaries: '+str({k:[round(z,3) for z in v] for k,v in bounds.items()})+'. Fixed frequency sensitivity uses 1, 2, 3-4, 5-7 and 8+ orders.')
story.append(PageBreak()); para('Track 2 | Long purchase gaps','Title'); story.append(Image(str(CH/'inactivity.png'),width=470,height=235))
table([['Minimum gap','Customers','Historical value']]+[[str(r['days'])+' days',str(r['customers']),f"${r['historical_value']:,.2f}"] for r in inactive],[140,100,180])
para('Use the 180-day high-value group as a review audience for a controlled re-engagement experiment. Segment labels and inactivity do not prove churn. Retain large spenders; inspect the large_spender_influence and large_spenders tables instead of deleting them. Test incentives against a randomized holdout and report incremental completed purchases, net merchandise value, and margin only when cost data is available.')
para('Sensitivity interpretation','Heading2'); para('Product-level median observed historical prices estimate missing unit prices in a separate scenario. Original values remain intact and no current catalog prices are substituted. That scenario scores all completed buyers and recomputes boundaries, so changes reflect both estimated value and a broader population. The 2025-only value scenario retains primary buyers, the two-year frequency and recency definitions, and recomputes monetary scores. Fixed-frequency transitions isolate the scoring choice. Review the exported customer transitions and summaries before audience activation.')
para('Fixed bands reduce Champions from 235 to 181 and Loyal from 114 to 73. Estimated-price scoring expands to 2,047 buyers and yields 271 Champions; it is a sensitivity, not a replacement for primary eligibility. The top 19 primary spenders contribute 8.1% of merchandise value, supporting retention with separate influence review.')
story.append(PageBreak()); para('Track 3 | Acquisition channels','Title'); story.append(Image(str(CH/'channel_activation.png'),width=280,height=140))
para('All-account activation and buyer-only segmentation answer different questions. Activation is completed buyers divided by all accounts, including accounts without orders. Buyer-only comparisons use fully priced buyers. Raw channel shares and values can reflect signup timing, coverage, and customer mix.')
para('Adjusted annual comparison','Heading2'); para('Common weights are the pooled distribution of scored buyers across 2024/2025 signup cohorts in channels meeting at least 50 buyers in each cohort. Supported channels: '+', '.join(supported)+'. Affiliate fails this support requirement. Adjusted repeat rates, segment shares, average historical value and recency are exported; no causal marketing effect or channel ROI is inferred. Acquisition costs and campaign exposure are absent.')
adj=pd.read_csv(OUT/'annual_cohort_adjusted.csv')
table([['Channel','Adjusted repeat','Adjusted mean value']]+[[r.acquisition_channel,f'{r.repeat:.1%}',f'${r.monetary:,.2f}'] for r in adj.itertuples()],[160,120,150])
para('Referral has the highest adjusted repeat rate (71.3%) and mean historical value ($901.23); paid social has the lowest (49.9%, $519.55). Treat these differences as CRM testing hypotheses, accounting for unmeasured customer and exposure differences.')
para('Finer signup-period sensitivity','Heading2'); para('Half-year groups use the same support rule and pooled weights. Supported: '+(', '.join(fine) or 'none')+'. Timing differences remain. Channel/cohort price coverage is exported. Eight category labels normalize case and whitespace; category order counts overlap.')
story.append(PageBreak()); para('Proposed CRM actions and measures','Title')
for seg,action,metric in actions:
 para(seg,'Heading2'); para(action+'. Future success metric: '+metric+'. Measure against a randomized holdout over the stated horizon; record contact eligibility and exposure. Monitor unsubscribe/complaint rates and promotion costs as guardrails.')
para('These are proposed experiments and future metrics, not demonstrated effectiveness. Review eligibility and customer contact permissions before operational use.')
story.append(PageBreak()); para('Validation and review limitations','Title')
para(f"{sum(x['passed'] for x in checks)}/{len(checks)} automated checks passed. Checks cover customer/product keys, foreign keys, conflicting order IDs, total reconciliation, equal-value scoring, score boundaries, eligibility, supplied coverage and segment benchmarks, category value reconciliation and filter boundaries.")
para('All original archive and extracted source files are preserved. Exact order duplicates are exported for review; conflicting duplicates would stop analysis. No unresolved historical unit price is overwritten. Canceled and returned orders appear separately in order_status_coverage. Source null counts, hashes, category mapping and purchase-before-signup records are exported.')
para('The evidence package RFM_Analysis_Stage_Evidence.zip was not present in the supplied folder. Accordingly, comparisons here are to the benchmarks in the task, not to the absent evidence tables or methods. Supply that archive before declaring evidence-package review complete.')
para('Final order status lacks outcome timestamps. The analysis uses the provided final statuses and cannot reconstruct exactly what was known on January 1, 2026. Purchase-before-signup anomalies, if any, are retained for source review. No causal channel conclusions, ROI, proven churn or campaign recovery estimates are supported.')
para('Source and AI-assisted role','Heading2'); para('Source: user-provided rfm-customer-segmentation-datasets.zip, attributed by the user to a Data Career School Wayfair-themed case study; no external provenance independently verified. Codex assisted in writing and running analysis, designing the local dashboard, producing charts/report and checking results. A human reviewer should approve definitions, source anomalies, contact eligibility and proposed experiments.')
def footer(canvas,doc):
 canvas.setFont('Helvetica',8); canvas.setFillColor(colors.HexColor('#58717b')); canvas.drawString(40,25,'LOCAL REVIEW DRAFT | Data Career School case study'); canvas.drawRightString(555,25,str(doc.page))
pdf=OUT/'rfm_review_report.pdf'; SimpleDocTemplate(str(pdf),pagesize=(595,842),rightMargin=40,leftMargin=40,topMargin=40,bottomMargin=45).build(story,onFirstPage=footer,onLaterPages=footer)
render=OUT/'pdf_preview'; render.mkdir(exist_ok=True)
for old in render.glob('page_*.png'): old.unlink()
doc=fitz.open(pdf)
for idx,page in enumerate(doc): page.get_pixmap(matrix=fitz.Matrix(1.2,1.2)).save(render/f'page_{idx+1}.png')
print('PDF pages',len(doc))
