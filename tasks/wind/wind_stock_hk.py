# -*- coding: utf-8 -*-
"""
Created on 2018/1/17
@author: MG
"""
import math
import logging
import pandas as pd
from datetime import date, datetime, timedelta
from tasks.backend import engine_md
from tasks.wind import invoker
from tasks.utils.db_utils import with_db_session, add_col_2_table
from tasks.utils.fh_utils import STR_FORMAT_DATE
from direstinvoker.ifind import APIError, UN_AVAILABLE_DATE
from tasks.utils.fh_utils import get_last, get_first, date_2_str
from sqlalchemy.types import String, Date, Float, Integer
from sqlalchemy.dialects.mysql import DOUBLE
from tasks.merge.code_mapping import update_from_info_table
from tasks.utils.db_utils import bunch_insert_on_duplicate_update
DEBUG = False
logger = logging.getLogger()
DATE_BASE = datetime.strptime('1980-01-01', STR_FORMAT_DATE).date()
ONE_DAY = timedelta(days=1)
# 标示每天几点以后下载当日行情数据
BASE_LINE_HOUR = 20


def get_stock_code_set(date_fetch):
    """
    :param date_fetch:
    :return:
    """
    date_fetch_str = date_fetch.strftime(STR_FORMAT_DATE)
    stock_df = invoker.wset("sectorconstituent", "date=%s;sectorid=a002010100000000" % date_fetch_str)  # 全部港股
    if stock_df is None:
        logging.warning('%s 获取股票代码失败', date_fetch_str)
        return None
    stock_count = stock_df.shape[0]
    logging.info('get %d stocks on %s', stock_count, date_fetch_str)
    return set(stock_df['wind_code'])


def import_wind_stock_info_hk(refresh=False):
    """
    获取全市场股票代码及名称 导入 港股股票信息 到 wind_stock_info_hk
    :param refresh: 默认为False，True 则进行全部更新
    :return:
    """
    table_name = 'wind_stock_info_hk'
    logging.info("更新 %s 开始", table_name)
    wind_indicator_param_list = [
        ('sec_name', String(10)),
        ('trade_code', String(20)),
        ('ipo_date', Date),
        ('delist_date', Date),
        ('mkt', String(20)),
        ('exch_city', String(100)),
        ('exch_eng', String(50)),
        ('prename', String(100)),
    ]
    param = ",".join([key for key, _ in wind_indicator_param_list])
    # 设置 dtype
    dtype = {key: val for key, val in wind_indicator_param_list}
    dtype['wind_code'] = String(20)
    dtype['ipo_date'] = Date

    if refresh:
        date_fetch = DATE_BASE
    else:
        date_fetch = date.today()
    date_end = date.today()
    stock_code_set = set()
    while date_fetch < date_end:
        stock_code_set_sub = get_stock_code_set(date_fetch)
        if stock_code_set_sub is not None:
            stock_code_set |= stock_code_set_sub
        date_fetch += timedelta(days=365)
    stock_code_set_sub = get_stock_code_set(date_end)

    if stock_code_set_sub is not None:
        stock_code_set |= stock_code_set_sub

    # 获取股票对应上市日期，及摘牌日期
    # w.wss("300005.SZ,300372.SZ,000003.SZ", "ipo_date,trade_code,mkt,exch_city,exch_eng")
    stock_code_list = list(stock_code_set)
    stock_code_count = len(stock_code_list)
    seg_count = 1000
    loop_count = math.ceil(float(stock_code_count) / seg_count)
    stock_info_df_list = []
    for n in range(loop_count):
        num_start = n * seg_count
        num_end = (n + 1) * seg_count
        num_end = num_end if num_end <= stock_code_count else stock_code_count
        stock_code_list_sub = stock_code_list[num_start:num_end]
        # 尝试将 stock_code_list_sub 直接传递给wss，是否可行
        # stock_info_df = invoker.wss(stock_code_list_sub,
        #                             "sec_name,trade_code,ipo_date,delist_date,mkt,exch_city,exch_eng,prename")
        stock_info_df = invoker.wss(stock_code_list_sub, param)
        stock_info_df_list.append(stock_info_df)
    stock_info_all_df = pd.concat(stock_info_df_list)
    stock_info_all_df.index.rename('WIND_CODE', inplace=True)
    logging.info('%s stock data will be import', stock_info_all_df.shape[0])
    stock_info_all_df.reset_index(inplace=True)
    # data_list = list(stock_info_all_df.T.to_dict().values())
    #  sql_str = "REPLACE INTO {table_name} (wind_code, trade_code, sec_name, ipo_date, delist_date, mkt, exch_city, exch_eng, prename) values (:WIND_CODE, :TRADE_CODE, :SEC_NAME, :IPO_DATE, :DELIST_DATE, :MKT, :EXCH_CITY, :EXCH_ENG, :PRENAME)".format(
    #      table_name=table_name
    #  )
    # # sql_str = "insert INTO wind_stock_info_hk (wind_code, trade_code, sec_name, ipo_date, delist_date, mkt, exch_city, exch_eng, prename) values (:WIND_CODE, :TRADE_CODE, :SEC_NAME, :IPO_DATE, :DELIST_DATE, :MKT, :EXCH_CITY, :EXCH_ENG, :PRENAME)"
    #  with with_db_session(engine_md) as session:
    #      session.execute(sql_str, data_list)
    #      stock_count = session.execute('select count(*) from {table_name}'.format(table_name=table_name)).first()[0]
    bunch_insert_on_duplicate_update(stock_info_all_df, table_name, engine_md,dtype=dtype)
    logging.info("更新 %s 完成 存量数据 %d 条", table_name,num_end)
    update_from_info_table(table_name)


def import_stock_daily_hk():
    """
    插入股票日线数据到最近一个工作日-1。
    如果超过 BASE_LINE_HOUR 时间，则获取当日的数据
    :return: 
    """
    table_name = 'wind_stock_daily_hk'
    logging.info("更新 wind_stock_daily_hk 开始")
    param_list = [
         ('OPEN', DOUBLE),
         ('HIGH', DOUBLE),
         ('LOW', DOUBLE),
         ('CLOSE', DOUBLE),
         ('ADJFACTOR', DOUBLE),
         ('VOLUME', DOUBLE),
         ('AMT', DOUBLE),
         ('PCT_CHG', DOUBLE),
         ('MAXUPORDOWN', Integer),
         ('SWING', DOUBLE),
         ('TURN', DOUBLE),
         ('FREE_TURN', DOUBLE),
         ('TRADE_STATUS', String(20)),
         ('SUSP_DAYS', Integer),
         ('TOTAL_SHARES', DOUBLE),
         ('FREE_FLOAT_SHARES', DOUBLE),
         ('EV2_TO_EBITDA', DOUBLE),
         ('PS_TTM', DOUBLE),
         ('PE_TTM', DOUBLE),
         ('PB_MRQ', DOUBLE),
    ]
    #将列表列名转化为小写
    col_name_dic = {col_name.upper(): col_name.lower() for col_name in param_list.keys()}
    # 获取列表列名
    col_name_list = [col_name.lower() for col_name in col_name_dic.keys()]
    # wind_indictor_str = "open,high,low,close,adjfactor,volume,amt,pct_chg,maxupordown," + \
    #                     "swing,turn,free_turn,trade_status,susp_days," + \
    #                     "total_shares,free_float_shares,ev2_to_ebitda"
    wind_indictor_str = ",".join(col_name_list)
    has_table = engine_md.has_table(table_name)
    if has_table:
        sql_str = """
           SELECT wind_code, date_frm, if(delist_date<end_date, delist_date, end_date) date_to
           FROM
           (
               SELECT info.wind_code, ifnull(trade_date, ipo_date) date_frm, delist_date,
               if(hour(now())<16, subdate(curdate(),1), curdate()) end_date
               FROM 
                   wind_stock_info_hk info 
               LEFT OUTER JOIN
                   (SELECT wind_code, adddate(max(trade_date),1) trade_date FROM {table_name} GROUP BY wind_code) daily
               ON info.wind_code = daily.wind_code
           ) tt
           WHERE date_frm <= if(delist_date<end_date, delist_date, end_date) 
           ORDER BY wind_code""".format(table_name=table_name)
    else:
        logger.warning('wind_stock_daily_hk 不存在，仅使用 wind_stock_info_hk 表进行计算日期范围')
        sql_str = """
           SELECT wind_code, date_frm, if(delist_date<end_date, delist_date, end_date) date_to
           FROM
           (
               SELECT info.wind_code, ipo_date date_frm, delist_date,
               if(hour(now())<16, subdate(curdate(),1), curdate()) end_date
               FROM wind_stock_info_hk info 
           ) tt
           WHERE date_frm <= if(delist_date<end_date, delist_date, end_date) 
           ORDER BY wind_code"""
        logger.warning('%s 不存在，仅使用 wind_stock_info_hk 表进行计算日期范围', table_name)
    with with_db_session(engine_md) as session:
        # 获取每只股票需要获取日线数据的日期区间
        table = session.execute(sql_str)
        # 获取每只股票需要获取日线数据的日期区间
        begin_time=None
        stock_date_dic = {
            wind_code: (date_from if begin_time is None else min([date_from, begin_time]), date_to)
            for wind_code, date_from, date_to in table.fetchall() if
            wind_code_set is None or wind_code in wind_code_set}
    # 设置 dtype
    dtype = {key: val for key, val in param_list}
    dtype['wind_code'] = String(20)
    dtype['trade_date'] = Date
    #
    # with with_db_session(engine_md) as session:
    #     # 获取每只股票最新交易日数据
    #     sql_str = 'select wind_code, max(Trade_date) from wind_stock_daily_hk group by wind_code'
    #     table = session.execute(sql_str)
    #     stock_trade_date_latest_dic = dict(table.fetchall())
    #     # 获取市场有效交易日数据
    #     # sql_str = "select trade_date from wind_trade_date_hk where trade_date > '1980-1-1'"
    #     # table = session.execute(sql_str)
    #     # trade_date_sorted_list = [t[0] for t in table.fetchall()]
    #     # trade_date_sorted_list.sort()
    #     # logger.info("加载交易日数据完成，最小交易日 %s", trade_date_sorted_list[0])
    #     # 获取每只股票上市日期、退市日期
    #     table = session.execute('SELECT wind_code, ipo_date, delist_date FROM wind_stock_info_hk')
    #     stock_date_dic = {
    #         wind_code: (ipo_date, delist_date if delist_date is None or delist_date > UN_AVAILABLE_DATE else None) for
    #         wind_code, ipo_date, delist_date in table.fetchall()}
    # date_ending = date.today() - ONE_DAY if datetime.now().hour < BASE_LINE_HOUR else date.today()
    data_df_list = []
    data_len = len(stock_date_dic)
    logger.info('%d stocks will been import into wind_stock_daily_hk', data_len)
    try:
        for data_num, (wind_code, (date_from,date_to)) in enumerate(stock_date_dic.items()):
            logger.debug('%d/%d) %s [%s - %s]', data_num, data_len, wind_code, date_from, date_to)
            # if wind_code in ('0388.HK'):
            #     # 请求 0388.HK 总是网络超时
            #     continue
            # date_ipo, date_delist = date_pair
            # if date_ipo is None:
            #     date_ipo = DATE_BASE
            #     logger.warning("%d/%d) %s 没有缺少 date_ipo 字段，默认使用 %s", data_num, data_len, wind_code,
            #                    date_2_str(DATE_BASE))
            #     logger.warning("暂时将 date_ipo 情况跳过 日后在对该类数据进行补充")
            #     continue
            # # 获取 date_from
            # if wind_code in stock_trade_date_latest_dic:
            #     date_latest_t1 = stock_trade_date_latest_dic[wind_code] + ONE_DAY
            #     date_from = max([date_latest_t1, DATE_BASE, date_ipo])
            # else:
            #     date_from = max([DATE_BASE, date_ipo])
            # # date_from = get_first(trade_date_sorted_list, lambda x: x >= date_from)
            # # 获取 date_to
            # if date_delist is None:
            #     date_to = date_ending
            # else:
            #     date_to = min([date_delist, date_ending])
            # # date_to = get_last(trade_date_sorted_list, lambda x: x <= date_to)
            # if date_from is None or date_to is None or date_from > date_to:
            #     continue
            # 获取股票量价等行情数据
            try:
                data_df = invoker.wsd(wind_code, wind_indictor_str, date_from, date_to)
            except APIError as exp:
                logger.exception("%d/%d) %s 执行异常", data_num, data_len, wind_code)
                if exp.ret_dic.setdefault('error_code', 0) in (
                        -40520007,  # 没有可用数据
                        -40521009,  # 数据解码失败。检查输入参数是否正确，如：日期参数注意大小月月末及短二月
                ):
                    continue
                else:
                    break
            if data_df is None:
                logger.warning('%d/%d) %s has no data during %s %s', data_num, data_len, wind_code, date_from, date_to)
                continue
            logger.info('%d/%d) %d data of %s between %s and %s', data_num, data_len, data_df.shape[0], wind_code,
                        date_from, date_to)
            data_df['wind_code'] = wind_code
            data_df_list.append(data_df)
            # 仅供调试使用
            if len(data_df_list) > 4:
                break
    finally:
        # 导入数据库
        if len(data_df_list) > 0:
            data_df_all = pd.concat(data_df_list)
            data_df_all.index.rename('trade_date', inplace=True)
            data_df_all.reset_index(inplace=True)
            data_df_all.rename(columns=col_name_dic, inplace=True)
            data_df_all.set_index(['wind_code', 'trade_date'], inplace=True)
            bunch_insert_on_duplicate_update(data_df_all, table_name, engine_md, dtype=dtype)
            # data_df_all.to_sql('wind_stock_daily_hk', engine_md, if_exists='append',dtype=dtype
            #
            #                    )
            logging.info("更新 wind_stock_daily_hk 结束 %d 条信息被更新", data_df_all.shape[0])


def import_stock_quertarly_hk():
    """
    插入股票日线数据到最近一个工作日-1
    :return:
    """
    table_name="wind_stock_quertarly_hk"
    logging.info("更新 wind_stock_quertarly_hk 开始")
    has_table = engine_md.has_table(table_name)
    if has_table:
        sql_str = """
           SELECT wind_code, date_frm, if(delist_date<end_date, delist_date, end_date) date_to
           FROM
           (
               SELECT info.wind_code, ifnull(trade_date, ipo_date) date_frm, delist_date,
               if(hour(now())<16, subdate(curdate(),1), curdate()) end_date
               FROM 
                   wind_stock_info_hk info 
               LEFT OUTER JOIN
                   (SELECT wind_code, adddate(max(trade_date),1) trade_date FROM {table_name} GROUP BY ths_code) quertarly
               ON info.wind_code = quertarly.wind_code
           ) tt
           WHERE date_frm <= if(delist_date<end_date, delist_date, end_date) 
           ORDER BY wind_code""".format(table_name=table_name)
    else:
        logger.warning('wind_stock_quertarly_hk 不存在，仅使用 wind_stock_info_hk 表进行计算日期范围')
        sql_str = """
           SELECT wind_code, date_frm, if(delist_date<end_date, delist_date, end_date) date_to
           FROM
           (
               SELECT info.wind_code, ipo_date date_frm, delist_date,
               if(hour(now())<16, subdate(curdate(),1), curdate()) end_date
               FROM wind_stock_info_hk info 
           ) tt
           WHERE date_frm <= if(delist_date<end_date, delist_date, end_date) 
           ORDER BY wind_code"""
        logger.warning('%s 不存在，仅使用 wind_stock_info_hk 表进行计算日期范围', table_name)
    with with_db_session(engine_md) as session:
        # 获取每只股票需要获取日线数据的日期区间
        table = session.execute(sql_str)
        # 计算每只股票需要获取日线数据的日期区间
        begin_time = None
        # 获取date_from,date_to，将date_from,date_to做为value值
        stock_date_dic = {
            wind_code: (date_from if begin_time is None else min([date_from, begin_time]), date_to)
            for wind_code, date_from, date_to in table.fetchall() if
            wind_code_set is None or wind_code in wind_code_set}
    # with with_db_session(engine_md) as session:
    #     # 获取每只股票最新交易日数据
    #     sql_str = 'select wind_code, max(Trade_date) from wind_stock_quertarly_hk group by wind_code'
    #     table = session.execute(sql_str)
    #     stock_trade_date_latest_dic = dict(table.fetchall())
    #     # 获取市场有效交易日数据
    #     sql_str = "select trade_date from wind_trade_date_hk where trade_date > '2005-1-1'"
    #     table = session.execute(sql_str)
    #     trade_date_sorted_list = [t[0] for t in table.fetchall()]
    #     trade_date_sorted_list.sort()
    #     # 获取每只股票上市日期、退市日期
    #     table = session.execute('SELECT wind_code, ipo_date, delist_date FROM wind_stock_info_hk')
    #     stock_date_dic = {
    #         wind_code: (ipo_date, delist_date if delist_date is None or delist_date > UN_AVAILABLE_DATE else None) for
    #         wind_code, ipo_date, delist_date in table.fetchall()}
    # date_ending = date.today() - ONE_DAY if datetime.now().hour < BASE_LINE_HOUR else date.today()
    data_df_list = []
    logger.info('%d stocks will been import into wind_stock_quertarly_hk', len(stock_date_dic))
    # 获取股票量价等行情数据
    field_col_name_list = [
        ('roic_ttm', String(20)),
        ('yoyprofit', String(20)),
        ('ebit', String(20)),
        ('ebit2', String(20)),
        ('ebit2_ttm', String(20)),
        ('surpluscapitalps', String(10)),
        ('undistributedps', String(20)),
        ('stm_issuingdate', Date),
    ]
    dtype = {key: val for key, val in field_col_name_list}
    dtype['wind_code'] = String(20)
    dtype['trade_date'] = Date
    wind_indictor_str = ",".join(key for key, _ in field_col_name_list)
    upper_col_2_name_dic = {name.upper(): val for name, val in field_col_name_list}

    try:
        for stock_num, (wind_code, (date_from,date_to)) in enumerate(stock_date_dic.items()):
            # date_ipo, date_delist = date_pair
            # # 获取 date_from
            # if wind_code in stock_trade_date_latest_dic:
            #     date_latest_t1 = stock_trade_date_latest_dic[wind_code] + ONE_DAY
            #     date_from = max([date_latest_t1, DATE_BASE, date_ipo])
            # else:
            #     date_from = max([DATE_BASE, date_ipo])
            # date_from = get_first(trade_date_sorted_list, lambda x: x >= date_from)
            # # 获取 date_to
            # if date_delist is None:
            #     date_to = date_ending
            # else:
            #     date_to = min([date_delist, date_ending])
            # date_to = get_last(trade_date_sorted_list, lambda x: x <= date_to)
            # if date_from is None or date_to is None or date_from > date_to:
            #     continue
            # 获取股票量价等行情数据
            # w.wsd("002122.SZ", "roic_ttm,yoyprofit,ebit,ebit2,ebit2_ttm,surpluscapitalps,undistributedps,stm_issuingdate", "2012-12-31", "2017-12-06", "unit=1;rptType=1;Period=Q")
            data_df = invoker.wsd(wind_code, wind_indictor_str, date_from, date_to, "unit=1;rptType=1;Period=Q")
            if data_df is None:
                logger.warning('%d) %s has no data during %s %s', stock_num, wind_code, date_from, date_to)
                continue
            data_df.rename(columns=upper_col_2_name_dic, inplace=True)
            # 清理掉期间全空的行
            for trade_date in list(data_df.index):
                is_all_none = data_df.loc[trade_date].apply(lambda x: x is None).all()
                if is_all_none:
                    logger.warning("%s %s 数据全部为空", wind_code, trade_date)
                    data_df.drop(trade_date, inplace=True)
            logger.info('%d) %d data of %s between %s and %s', stock_num, data_df.shape[0], wind_code, date_from,
                        date_to)
            data_df['wind_code'] = wind_code
            data_df_list.append(data_df)
            # 仅供调试使用
            if DEBUG and len(data_df_list) > 5:
                break
    finally:
        # 导入数据库
        if len(data_df_list) > 0:
            data_df_all = pd.concat(data_df_list)
            data_df_all.index.rename('trade_date', inplace=True)
            data_df_all.reset_index(inplace=True)
            data_df_all.set_index(['wind_code', 'trade_date'], inplace=True)
            # data_df_all.to_sql('wind_stock_quertarly_hk', engine_md, if_exists='append')
            bunch_insert_on_duplicate_update(data_df_all, table_name, engine_md, dtype=dtype)
            logging.info("更新 wind_stock_quertarly_hk 结束 %d 条信息被更新", data_df_all.shape[0])


def fill_col():
    """补充历史col数据"""

    # 股票列表
    sql_str = """select *
    from (
    select wind_code, sum(amt) amt_tot
    from wind_stock_daily_hk
    where trade_date BETWEEN '2015-1-1' and '2017-12-31'
    GROUP BY wind_code
    ) tt
    order by amt_tot desc"""
    data_df = pd.read_sql(sql_str, engine_md, index_col='wind_code')
    # 由于数量比较大，目前只执行前 N 支股票
    stock_count = data_df.shape[0]
    first_n_count = 2200
    logger.info("共 %d 支股票需要更新，目前只更新前 %d 支", stock_count, first_n_count)
    wind_code_list = list(data_df[:first_n_count].index)

    col_name_dic = {'PS_TTM': 'PS',
                    'PE_TTM': 'PE',
                    'PB_MRQ': 'PB',
                    }
    col_name_list = [col_name.lower() for col_name in col_name_dic.keys()]
    # 获取每只股票ipo 日期 及 最小的交易日前一天
    #     sql_str = """select si.wind_code, td_from, td_to
    # from wind_stock_info si,
    # (select wind_code, min(trade_date) td_from, max(trade_date) td_to from wind_stock_daily where ev2_to_ebitda is null group by wind_code) sd
    # where si.wind_code = sd.wind_code"""
    sql_str = """
            select wsd.wind_code, min_trade_date, max_trade_date
            from
            (
            select wind_code, min(trade_date) min_trade_date, max(trade_date) max_trade_date, max(amt) amt_max
            from wind_stock_daily_hk
            group by wind_code
            ) wsd
            LEFT JOIN
            (
            select wind_code
            from wind_stock_daily_hk
            where PB is not null
            group by wind_code
            ) wsd_null
            on wsd.wind_code = wsd_null.wind_code
            where wsd_null.wind_code is null
            order by amt_max desc"""
    with with_db_session(engine_md) as session:
        table = session.execute(sql_str)
        stock_trade_date_range_dic = {content[0]: (content[1], content[2]) for content in table.fetchall()}
    data_df_list = []
    try:
        # for n, (wind_code, (date_from, date_to)) in enumerate(stock_trade_date_range_dic.items()):
        for data_num, wind_code in enumerate(wind_code_list, start=1):
            if wind_code not in stock_trade_date_range_dic:
                continue
            if wind_code == '8008.HK':  # 这支票数据有问题 实际应该是 '1686.HK' '新意网集团'
                continue
            date_from, date_to = stock_trade_date_range_dic[wind_code]
            # 获取股票量价等行情数据
            wind_indictor_str = col_name_list
            data_df = invoker.wsd(wind_code, wind_indictor_str, date_from, date_to)
            if data_df is None:
                logger.warning('%d) %s has no data during %s %s', data_num, wind_code, date_from, date_to)
                continue
            logger.info('%d) %d data of %s between %s and %s', data_num, data_df.shape[0], wind_code, date_from,
                        date_to)
            data_df['wind_code'] = wind_code
            data_df_list.append(data_df)
            # 仅供调试使用
            if data_num > 10:
                break
    finally:
        # 导入数据库
        if len(data_df_list) > 0:
            data_df_all = pd.concat(data_df_list)
            data_df_all.index.rename('trade_date', inplace=True)
            data_df_all.reset_index(inplace=True)
            # 只有全部列为空的项才需要剔除
            is_na_s = None
            for col_name in col_name_dic.keys():
                if is_na_s is None:
                    is_na_s = data_df_all[col_name].isna()
                else:
                    is_na_s = is_na_s & data_df_all[col_name].isna()
            data_df_not_null = data_df_all[~is_na_s]
            data_df_not_null.fillna('null', inplace=True)
            if data_df_not_null.shape[0] > 0:
                data_dic_list = data_df_not_null.to_dict(orient='records')
                sql_str = "update wind_stock_daily_hk set " + \
                          ",".join(
                              ["%s=:%s" % (db_col_name, col_name) for col_name, db_col_name in col_name_dic.items()]) + \
                          " where wind_code=:wind_code and trade_date=:trade_date"
                with with_db_session(engine_md) as session:
                    table = session.execute(sql_str, params=data_dic_list)
            logger.info('%d data imported', data_df_not_null.shape[0])
        else:
            logger.warning('no data for update')


def add_new_col_data(col_name, param, db_col_name=None, col_type_str='DOUBLE', wind_code_set: set = None):
    """
    1）修改 daily 表，增加字段
    2）wind_ckdvp_stock_hk表增加数据
    3）第二部不见得1天能够完成，当第二部完成后，将wind_ckdvp_stock_hk数据更新daily表中
    :param col_name:增加字段名称
    :param param: 参数
    :param db_col_name: 默认为 None，此时与col_name相同
    :param col_type_str: DOUBLE, VARCHAR(20), INTEGER, etc. 不区分大小写
    :param wind_code_set: 默认 None， 否则仅更新指定 wind_code
    :return:
    """
    if db_col_name is None:
        # 默认为 None，此时与col_name相同
        db_col_name = col_name

    # 检查当前数据库是否存在 db_col_name 列，如果不存在则添加该列
    add_col_2_table(engine_md, 'wind_stock_daily_hk', db_col_name, col_type_str)
    # 将数据增量保存到 wind_ckdvp_stock_hk 表
    all_finished = add_data_2_ckdvp(col_name, param, wind_code_set)
    # 将数据更新到 ds 表中
    # 对表的列进行整合，daily表的列属性值插入wind_ckdvp_stock_hk的value 根据所有条件进行判定
    if all_finished:
        sql_str = """
            update wind_stock_daily_hk daily, wind_ckdvp_stock_hk ckdvp
            set daily.{db_col_name} = ckdvp.value
            where daily.wind_code = ckdvp.wind_code
            and ckdvp.key = '{db_col_name}' and ckdvp.param = '{param}'
            and ckdvp.time = daily.trade_date""".format(db_col_name=db_col_name, param=param)
        # 进行事务提交
        with with_db_session(engine_md) as session:
            rst = session.execute(sql_str)
            data_count = rst.rowcount
            session.commit()
        logger.info('更新 %s 字段 wind_stock_daily_hk 表 %d 条记录', db_col_name, data_count)


def add_data_2_ckdvp(col_name, param, wind_code_set: set = None, begin_time=None):
    """判断表格是否存在，存在则进行表格的关联查询
    :param col_name: 增加的列属性名
    :param param: 参数
    :param wind_code_set: 默认为None
    :param begin_time: 默认为None
    :return:
    """
    table_name = 'wind_ckdvp_stock_hk'
    all_finished = False
    has_table = engine_md.has_table('wind_ckdvp_stock_hk')
    if has_table:
        # 执行语句，表格数据联立
        sql_str = """
            select wind_code, date_frm, if(delist_date<end_date, delist_date, end_date) date_to
            FROM
            (
                select info.wind_code,
                    (ipo_date) date_frm, delist_date,
                    if(hour(now())<16, subdate(curdate(),1), curdate()) end_date
                from wind_stock_info info
                left outer join
                    (select wind_code, adddate(max(time),1) from wind_ckdvp_stock
                    where wind_ckdvp_stock.key='{0}' and param='{1}' group by wind_code
                    ) daily
                on info.wind_code = daily.wind_code
            ) tt
            where date_frm <= if(delist_date<end_date,delist_date, end_date)
            order by wind_code""".format(col_name, param)
    else:
        logger.warning('wind_ckdvp_stock_hk 不存在，仅使用 wind_stock_info_hk 表进行计算日期范围')
        sql_str = """
            SELECT wind_code, date_frm,
                if(delist_date<end_date,delist_date, end_date) date_to
            FROM
            (
                SELECT info.wind_code,ipo_date date_frm, delist_date,
                if(hour(now())<16, subdate(curdate(),1), curdate()) end_date
                FROM wind_stock_info info
            ) tt
            WHERE date_frm <= if(delist_date<end_date, delist_date, end_date)
            ORDER BY wind_code"""
    with with_db_session(engine_md) as session:
        # 获取每只股票需要获取日线数据的日期区间
        table = session.execute(sql_str)
        code_date_range_dic = {
            wind_code: (date_from if begin_time is None else min([date_from, begin_time]), date_to)
            for wind_code, date_from, date_to in table.fetchall() if
            wind_code_set is None or wind_code in wind_code_set}

        # 设置 dtype
        dtype = {
            'wind_code': String(20),
            'key': String(80),
            'time': Date,
            'value': String(80),
            'param': String(80),

        }
        data_df_list, data_count, tot_data_count, code_count = [], 0, 0, len(code_date_range_dic)
        try:
            for num, (wind_code, (date_from, date_to)) in enumerate(code_date_range_dic.items(), start=1):
                logger.debug('%d/%d) %s [%s - %s]', num, code_count, wind_code, date_from, date_to)
                data_df = invoker.wsd(
                    wind_code,
                    col_name,
                    date_from,
                    date_to,
                    param
                )
                if data_df is not None or data_df.shape[0] > 0:
                    # 对我们的表格进行规范整理,整理我们的列名，索引更改
                    data_df['key'] = col_name
                    data_df['param'] = param
                    data_df['wind_code'] = wind_code
                    data_df.rename(columns={col_name.upper(): 'value'}, inplace=True)
                    data_df.index.rename('time', inplace=True)
                    data_df.reset_index(inplace=True)
                    data_count += data_df.shape[0]
                    data_df_list.append(data_df)

                # 大于阀值有开始插入
                if data_count >= 10000:
                    tot_data_df = pd.concat(data_df_list)
                    tot_data_df.to_sql(table_name, engine_md, if_exists='append', index=False, dtype=dtype)
                    tot_data_count += data_count
                    data_df_list, data_count = [], 0

                # 仅调试使用
                if DEBUG and len(data_df_list) > 3:
                    break

                all_finished = True
        finally:
            if data_count > 0:
                tot_data_df = pd.concat(data_df_list)
                bunch_insert_on_duplicate_update(tot_data_df, table_name, engine_md, dtype=dtype)
                # tot_data_df.to_sql(table_name, engine_md, if_exists='append', index=False, dtype=dtype)
                tot_data_count += data_count

            if not has_table and engine_md.has_table(table_name):
                create_pk_str = """ALTER TABLE {table_name}
                    CHANGE COLUMN `wind_code` `wind_code` VARCHAR(20) NOT NULL ,
                    CHANGE COLUMN `time` `time` DATE NOT NULL ,
                    CHANGE COLUMN `key` `key` VARCHAR(80) NOT NULL ,
                    CHANGE COLUMN `param` `param` VARCHAR(80) NOT NULL ,
                    ADD PRIMARY KEY (`wind_code`, `time`, `key`, `param`)""".format(table_name=table_name)
                with with_db_session(engine_md) as session:
                    session.execute(create_pk_str)

            logging.info("更新 %s 完成 新增数据 %d 条", table_name, tot_data_count)

        return all_finished


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s: %(levelname)s [%(name)s:%(funcName)s] %(message)s')
    DEBUG = True
    wind_code_set = {'1680.HK'}
    # wind_code_set = None
    # import_wind_stock_info_hk(refresh=False)
    # import_stock_daily_hk(wind_code_set)'1680.HK'
    # import_stock_quertarly_hk()
    # fill_col()
    add_new_col_data('ebitdaps', '', wind_code_set=wind_code_set)