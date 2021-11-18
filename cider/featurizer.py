from collections import defaultdict
import bandicoot as bc  # type: ignore[import]
from cider.datastore import DataStore, DataType
import geopandas as gpd  # type: ignore[import]
from helpers.utils import cdr_bandicoot_format, flatten_folder, flatten_lst, long_join_pyspark, long_join_pandas, \
    make_dir, save_df, save_parquet
from helpers.features import all_spark
from helpers.io_utils import get_spark_session
from helpers.plot_utils import clean_plot, dates_xaxis, distributions_plot
import json
import matplotlib.pyplot as plt  # type: ignore[import]
from multiprocessing import Pool
import os
import pandas as pd
from pandas import DataFrame as PandasDataFrame
from pyspark.sql import DataFrame as SparkDataFrame
from pyspark.sql.types import StringType
from pyspark.sql.functions import array, col, count, countDistinct, explode, first, lit, max, mean, min, stddev, sum
from pyspark.sql.utils import AnalysisException
import seaborn as sns  # type: ignore[import]
from typing import Any, Dict, List, Optional, Union


class Featurizer:

    def __init__(self,
                 datastore: DataStore,
                 dataframes: Optional[Dict[str, Optional[Union[PandasDataFrame, SparkDataFrame]]]] = None,
                 clean_folders: bool = False) -> None:
        self.cfg = datastore.cfg
        self.ds = datastore
        self.outputs = datastore.outputs + 'featurizer/'

        # Prepare working directories
        make_dir(self.outputs, clean_folders)
        make_dir(self.outputs + '/outputs/')
        make_dir(self.outputs + '/plots/')
        make_dir(self.outputs + '/tables/')

        self.features: Dict[str, Optional[SparkDataFrame]] = {'cdr': None, 'international': None, 'recharges': None,
                                                              'location': None, 'mobiledata': None, 'mobilemoney': None}

        # Spark setup
        # TODO(lucio): Initialize spark separately ....
        spark = get_spark_session(self.cfg)
        self.spark = spark

        # Create default dicts to avoid key errors
        dataframes = dataframes if dataframes else defaultdict(lambda: None)
        data_type_map = {DataType.CDR: dataframes['cdr'],
                         DataType.RECHARGES: dataframes['recharges'],
                         DataType.MOBILEDATA: dataframes['mobiledata'],
                         DataType.MOBILEMONEY: dataframes['mobilemoney'],
                         DataType.ANTENNAS: dataframes['antennas'],
                         DataType.SHAPEFILES: None}
        # Load data into datastore, initialize bandicoot attribute
        self.ds.load_data(data_type_map=data_type_map)
        self.ds.cdr_bandicoot = None

    def diagnostic_statistics(self, write: bool = True) -> Dict[str, Dict[str, int]]:
        """
        Compute summary statistics of datasets

        Args:
            write: whether to write json to disk

        Returns: dict of dicts containing summary stats - {'CDR': {'Transactions': 2.3, ...}, ...}
        """
        statistics: Dict[str, Dict[str, int]] = {}

        for name, df in [('CDR', self.ds.cdr),
                         ('Recharges', self.ds.recharges),
                         ('Mobile Data', self.ds.mobiledata),
                         ('Mobile Money', self.ds.mobilemoney)]:
            if df is not None:

                statistics[name] = {}

                # Number of days
                lastday = pd.to_datetime(df.agg({'timestamp': 'max'}).collect()[0][0])
                firstday = pd.to_datetime(df.agg({'timestamp': 'min'}).collect()[0][0])
                statistics[name]['Days'] = (lastday - firstday).days + 1

                # Number of transactions
                statistics[name]['Transactions'] = df.count()

                # Number of subscribers
                statistics[name]['Subscribers'] = df.select('caller_id').distinct().count()

                # Number of recipients
                if 'recipient_id' in df.columns:
                    statistics[name]['Recipients'] = df.select('recipient_id').distinct().count()

        if write:
            with open(self.outputs + '/tables/statistics.json', 'w') as f:
                json.dump(statistics, f)

        return statistics

    def diagnostic_plots(self, plot: bool = True) -> None:
        """
        Compute time series of transactions, save to disk, and plot if requested

        Args:
            plot: whether to plot graphs
        """
        for name, df in [('CDR', self.ds.cdr),
                         ('Recharges', self.ds.recharges),
                         ('Mobile Data', self.ds.mobiledata),
                         ('Mobile Money', self.ds.mobilemoney)]:
            if df is not None:

                if 'txn_type' not in df.columns:
                    df = df.withColumn('txn_type', lit('txn'))

                # Save timeseries of transactions by day
                save_df(df.groupby(['txn_type', 'day']).count(),
                        self.outputs + '/datasets/' + name.replace(' ', '') + '_transactionsbyday.csv')

                # Save timeseries of subscribers by day
                save_df(df
                        .groupby(['txn_type', 'day'])
                        .agg(countDistinct('caller_id'))
                        .withColumnRenamed('count(caller_id)', 'count'),
                        self.outputs + '/datasets/' + name.replace(' ', '') + '_subscribersbyday.csv')

                if plot:
                    # Plot timeseries of transactions by day
                    timeseries = pd.read_csv(
                        self.outputs + '/datasets/' + name.replace(' ', '') + '_transactionsbyday.csv')
                    timeseries['day'] = pd.to_datetime(timeseries['day'])
                    timeseries = timeseries.sort_values('day', ascending=True)
                    fig, ax = plt.subplots(1, figsize=(20, 6))
                    for txn_type in timeseries['txn_type'].unique():
                        subset = timeseries[timeseries['txn_type'] == txn_type]
                        ax.plot(subset['day'], subset['count'], label=txn_type)
                        ax.scatter(subset['day'], subset['count'], label='')
                    if len(timeseries['txn_type'].unique()) > 1:
                        ax.legend(loc='best')
                    ax.set_title(name + ' Transactions by Day', fontsize='large')
                    dates_xaxis(ax, frequency='week')
                    clean_plot(ax)
                    plt.savefig(self.outputs + '/plots/' + name.replace(' ', '') + '_transactionsbyday.png', dpi=300)

                    # Plot timeseries of subscribers by day
                    timeseries = pd.read_csv(
                        self.outputs + '/datasets/' + name.replace(' ', '') + '_subscribersbyday.csv')
                    timeseries['day'] = pd.to_datetime(timeseries['day'])
                    timeseries = timeseries.sort_values('day', ascending=True)
                    fig, ax = plt.subplots(1, figsize=(20, 6))
                    for txn_type in timeseries['txn_type'].unique():
                        subset = timeseries[timeseries['txn_type'] == txn_type]
                        ax.plot(subset['day'], subset['count'], label=txn_type)
                        ax.scatter(subset['day'], subset['count'], label='')
                    if len(timeseries['txn_type'].unique()) > 1:
                        ax.legend(loc='best')
                    ax.set_title(name + ' Subscribers by Day', fontsize='large')
                    dates_xaxis(ax, frequency='week')
                    clean_plot(ax)
                    plt.savefig(self.outputs + '/plots/' + name.replace(' ', '') + '_subscribersbyday.png', dpi=300)

    def cdr_features(self, bc_chunksize: int = 500000, bc_processes: int = 55) -> None:
        """
        Compute CDR features using bandicoot library and save to disk

        Args:
            bc_chunksize: number of users per chunk
            bc_processes: number of processes to run in parallel
        """
        # Check that CDR is present to calculate international features
        if self.ds.cdr is None:
            raise ValueError('CDR file must be loaded to calculate CDR features.')
        print('Calculating CDR features...')

        # Convert CDR into bandicoot format
        self.ds.cdr_bandicoot = cdr_bandicoot_format(self.ds.cdr, self.ds.antennas, self.cfg.col_names.cdr)

        # Get list of unique subscribers, write to file
        save_df(self.ds.cdr_bandicoot.select('name').distinct(), self.outputs + '/datasets/subscribers.csv')
        subscribers = self.ds.cdr_bandicoot.select('name').distinct().rdd.map(lambda r: r[0]).collect()

        # Make adjustments to chunk size and parallelization if necessary
        if bc_chunksize > len(subscribers):
            bc_chunksize = len(subscribers)
        if bc_processes > int(len(subscribers) / bc_chunksize):
            bc_processes = int(len(subscribers) / bc_chunksize)

        # Make output folders
        make_dir(self.outputs + '/datasets/bandicoot_records')
        make_dir(self.outputs + '/datasets/bandicoot_features')

        old_outputs_name = self.outputs
        self.outputs = self.outputs.split('://')[-1]

        # Get bandicoot features in chunks
        start = 0
        end = 0
        while end < len(subscribers):

            # Get start and end point of chunk
            end = start + bc_chunksize
            chunk = subscribers[start:end]

            # Name outfolders
            recs_folder = self.outputs + '/datasets/bandicoot_records/' + str(start) + 'to' + str(end)
            bc_folder = self.outputs + '/datasets/bandicoot_features/' + str(start) + 'to' + str(end)
            print(f'Making {bc_folder}')
            make_dir(bc_folder)

            # Get records for this chunk and write out to csv files per person
            nums_spark = self.spark.createDataFrame(chunk, StringType()).withColumnRenamed('value', 'name')
            matched_chunk = self.ds.cdr_bandicoot.join(nums_spark, on='name', how='inner')
            matched_chunk.repartition('name').write.partitionBy('name').mode('append').format('csv').save(recs_folder,
                                                                                                          header=True)

            # Move csv files around on disk to get into position for bandicoot
            n = int(len(chunk) / bc_processes)
            subchunks = [chunk[i:i + n] for i in range(0, len(chunk), n)]
            pool = Pool(bc_processes)
            unmatched = pool.map(flatten_folder, [(subchunk, recs_folder) for subchunk in subchunks])
            unmatched = flatten_lst(unmatched)
            pool.close()
            if len(unmatched) > 0:
                print('Warning: lost %i subscribers in file shuffling' % len(unmatched))

            # Calculate bandicoot features
            def get_bc(sub: Any) -> Any:
                return bc.utils.all(bc.read_csv(str(sub), recs_folder, describe=True), summary='extended',
                                    split_week=True,
                                    split_day=True, groupby=None)

            # Write out bandicoot feature files
            def write_bc(index: Any, iterator: Any) -> Any:
                bc.to_csv(list(iterator), bc_folder + '/' + str(index) + '.csv')
                return ['index: ' + str(index)]

            # Run calculations and writing of bandicoot features in parallel
            feature_df = self.spark.sparkContext.emptyRDD()
            subscriber_rdd = self.spark.sparkContext.parallelize(chunk)
            features = subscriber_rdd.mapPartitions(
                lambda s: [get_bc(sub) for sub in s if os.path.isfile(recs_folder + '/' + sub + '.csv')])
            feature_df = feature_df.union(features)
            out = feature_df.coalesce(bc_processes).mapPartitionsWithIndex(write_bc)
            out.count()
            start = start + bc_chunksize

        self.outputs = old_outputs_name

        # Combine all bandicoot features into a single file, fix column names, and write to disk
        cdr_features = self.spark.read.csv(self.outputs + '/datasets/bandicoot_features/*/*', header=True)
        cdr_features = cdr_features.select([col for col in cdr_features.columns if
                                            ('reporting' not in col) or (col == 'reporting__number_of_records')])
        cdr_features = cdr_features.toDF(*[c if c == 'name' else 'cdr_' + c for c in cdr_features.columns])
        save_df(cdr_features, self.outputs + '/datasets/bandicoot_features/all.csv')
        self.features['cdr'] = self.spark.read.csv(self.outputs + '/datasets/bandicoot_features/all.csv',
                                                   header=True, inferSchema=True)

    def cdr_features_spark(self) -> None:
        """
        Compute CDR features using spark and save to disk
        """
        # Check that CDR is present to calculate international features
        if self.ds.cdr is None:
            raise ValueError('CDR file must be loaded to calculate CDR features.')
        print('Calculating CDR features...')

        cdr_features = all_spark(self.ds.cdr, self.ds.antennas, cfg=self.cfg.params.cdr)
        cdr_features_df = long_join_pyspark(cdr_features, on='caller_id', how='outer')
        cdr_features_df = cdr_features_df.withColumnRenamed('caller_id', 'name')

        save_df(cdr_features_df, self.outputs + '/datasets/cdr_features_spark/all.csv')
        self.features['cdr'] = self.spark.read.csv(self.outputs + '/datasets/cdr_features_spark/all.csv',
                                                   header=True, inferSchema=True)

    def international_features(self) -> None:
        # Check that CDR is present to calculate international features
        if self.ds.cdr is None:
            raise ValueError('CDR file must be loaded to calculate international features.')
        print('Calculating international features...')

        # Write international transactions to file
        international_trans = self.ds.cdr.filter(col('international') == 'international')
        save_df(international_trans, self.outputs + '/datasets/internatonal_transactions.csv')

        # Read international calls
        old_outputs_name = self.outputs
        self.outputs = self.outputs.split('://')[-1]
        inter = pd.read_csv(self.outputs + '/datasets/internatonal_transactions.csv')

        # Calculate list of aggregations by subscriber
        inter_voice = inter[inter['txn_type'] == 'call']
        inter_sms = inter[inter['txn_type'] == 'text']
        lst = [
            ('recipient_id', ['count', 'nunique']),
            ('day', ['nunique']),
            ('duration', ['sum'])
        ]
        feats = []
        for c, agg in lst:
            for subset, name in [(inter, 'all'), (inter_voice, 'call'), (inter_sms, 'text')]:
                grouped = subset[['caller_id', c]].groupby('caller_id', as_index=False).agg(agg)
                grouped.columns = [name + '__' + c + '__' + ag for ag in agg]
                feats.append(grouped)

        # Combine all aggregations together, write to file
        feats_df= long_join_pandas(feats, on='caller_id', how='outer').rename({'caller_id': 'name'}, axis=1)
        feats_df['name'] = feats_df.index
        feats_df.columns = [c if c == 'name' else 'international_' + c for c in feats_df.columns]
        feats_df.to_csv(self.outputs + '/datasets/international_feats.csv', index=False)
        
        self.outputs = old_outputs_name
        self.features['international'] = self.spark.read.csv(self.outputs + '/datasets/international_feats.csv',
                                                             header=True, inferSchema=True)

    def location_features(self) -> None:

        # Check that antennas and CDR are present to calculate spatial features
        if self.ds.cdr is None:
            raise ValueError('CDR file must be loaded to calculate spatial features.')
        if self.ds.antennas is None:
            raise ValueError('Antenna file must be loaded to calculate spatial features.')
        print('Calculating spatial features...')

        # If CDR is not available in bandicoot format, calculate it
        if self.ds.cdr_bandicoot is None:
            self.ds.cdr_bandicoot = cdr_bandicoot_format(self.ds.cdr, self.ds.antennas, self.cfg.col_names.cdr)

        # Get dataframe of antennas located within regions
        antennas = pd.read_csv(self.ds.data + self.ds.file_names.antennas)
        antennas = gpd.GeoDataFrame(antennas, geometry=gpd.points_from_xy(antennas['longitude'], antennas['latitude']))
        antennas.crs = {"init": "epsg:4326"}
        antennas = antennas[antennas.is_valid]
        for shapefile_name in self.ds.shapefiles.keys():
            shapefile = self.ds.shapefiles[shapefile_name].rename({'region': shapefile_name}, axis=1)
            antennas = gpd.sjoin(antennas, shapefile, op='within', how='left').drop('index_right', axis=1)
            antennas[shapefile_name] = antennas[shapefile_name].fillna('Unknown')
        antennas = self.spark.createDataFrame(antennas.drop(['geometry', 'latitude', 'longitude'], axis=1).fillna(''))

        # Merge CDR to antennas
        cdr = self.ds.cdr_bandicoot.join(antennas, on='antenna_id', how='left') \
            .na.fill({shapefile_name: 'Unknown' for shapefile_name in self.ds.shapefiles.keys()})

        # Get counts by region
        for shapefile_name in self.ds.shapefiles.keys():
            countbyregion = cdr.groupby(['name', shapefile_name]).count()
            save_df(countbyregion, self.outputs + '/datasets/countby' + shapefile_name + '.csv')

        # Get unique regions (and unique towers)
        unique_regions = cdr.select('name').distinct()
        for shapefile_name in self.ds.shapefiles.keys():
            unique_regions = unique_regions.join(cdr.groupby('name').agg(countDistinct(shapefile_name)), on='name',
                                                 how='left')
        if 'tower_id' in cdr.columns:
            unique_regions = unique_regions.join(cdr.groupby('name').agg(countDistinct('tower_id')), on='name',
                                                 how='left')
        save_df(unique_regions, self.outputs + '/datasets/uniqueregions.csv')

        # Pivot counts by region
        count_by_region_compiled = []
        for shapefile_name in self.ds.shapefiles.keys():
            count_by_region = pd.read_csv(self.outputs + '/datasets/countby' + shapefile_name + '.csv') \
                .pivot(index='name', columns=shapefile_name, values='count').fillna(0)
            count_by_region['total'] = count_by_region.sum(axis=1)
            for c in set(count_by_region.columns) - {'total', 'name'}:
                count_by_region[c + '_percent'] = count_by_region[c] / count_by_region['total']
            count_by_region = count_by_region.rename(
                {region: shapefile_name + '_' + region for region in count_by_region.columns}, axis=1)
            count_by_region_compiled.append(count_by_region)

        count_by_region = long_join_pandas(count_by_region_compiled, on='name', how='outer')
        count_by_region = count_by_region.drop([c for c in count_by_region.columns if 'total' in c], axis=1)

        # Read in the unique regions
        unique_regions = pd.read_csv(self.outputs + '/datasets/uniqueregions.csv')

        # Merge counts and unique counts together, write to file
        feats = count_by_region.merge(unique_regions, on='name', how='outer')
        feats.columns = [c if c == 'name' else 'location_' + c for c in feats.columns]
        feats.to_csv(self.outputs.split('://')[-1] + '/datasets/location_features.csv', index=False)
        self.features['location'] = self.spark.read.csv(self.outputs + '/datasets/location_features.csv',
                                                        header=True, inferSchema=True)

    def mobiledata_features(self) -> None:

        # Check that mobile internet data is loaded
        if self.ds.mobiledata is None:
            raise ValueError('Mobile data file must be loaded to calculate mobile data features.')
        print('Calculating mobile data features...')

        # Perform set of aggregations on mobile data 
        feats = self.ds.mobiledata.groupby('caller_id').agg(sum('volume').alias('total_volume'),
                                                            mean('volume').alias('mean_volume'),
                                                            min('volume').alias('min_volume'),
                                                            max('volume').alias('max_volume'),
                                                            stddev('volume').alias('std_volume'),
                                                            countDistinct('day').alias('num_days'),
                                                            count('volume').alias('num_transactions'))

        # Save to file
        feats = feats.withColumnRenamed('caller_id', 'name')
        feats = feats.toDF(*[c if c == 'name' else 'mobiledata_' + c for c in feats.columns])
        self.features['mobiledata'] = feats
        save_df(feats, self.outputs + '/datasets/mobiledata_features.csv')

    def mobilemoney_features(self) -> None:

        # Check that mobile money is loaded
        if self.ds.mobilemoney is None:
            raise ValueError('Mobile money file must be loaded to calculate mobile money features.')
        print('Calculating mobile money features...')

        # Get outgoing transactions
        sender_cols = ['txn_type', 'caller_id', 'recipient_id', 'day', 'amount', 'sender_balance_before',
                       'sender_balance_after']
        outgoing = (self.ds.mobilemoney
                    .select(sender_cols)
                    .withColumnRenamed('caller_id', 'name')
                    .withColumnRenamed('recipient_id', 'correspondent_id')
                    .withColumnRenamed('sender_balance_before', 'balance_before')
                    .withColumnRenamed('sender_balance_after', 'balance_after')
                    .withColumn('direction', lit('out')))

        # Get incoming transactions
        recipient_cols = ['txn_type', 'caller_id', 'recipient_id', 'day', 'amount', 'recipient_balance_before',
                          'recipient_balance_after']
        incoming = (self.ds.mobilemoney.select(recipient_cols)
                    .withColumnRenamed('recipient_id', 'name')
                    .withColumnRenamed('caller_id', 'correspondent_id')
                    .withColumnRenamed('recipient_balance_before', 'balance_before')
                    .withColumnRenamed('recipient_balance_after', 'balance_after')
                    .withColumn('direction', lit('in')))

        # Combine incoming and outgoing with unified schema
        mm = outgoing.select(incoming.columns).union(incoming)
        save_parquet(mm, self.outputs + '/datasets/mobilemoney')
        mm = self.spark.read.parquet(self.outputs + '/datasets/mobilemoney')
        outgoing = mm.where(col('direction') == 'out')
        incoming = mm.where(col('direction') == 'in')

        # Get mobile money features
        features = []
        for dfname, df in [('all', mm), ('incoming', incoming), ('outgoing', outgoing)]:
            # add 'all' txn type
            df = (df
                  .withColumn('txn_types', array(lit('all'), col('txn_type')))
                  .withColumn('txn_type', explode('txn_types')))

            aggs = (df
                    .groupby('name', 'txn_type')
                    .agg(mean('amount').alias('amount_mean'),
                         min('amount').alias('amount_min'),
                         max('amount').alias('amount_max'),
                         mean('balance_before').alias('balance_before_mean'),
                         min('balance_before').alias('balance_before_min'),
                         max('balance_before').alias('balance_before_max'),
                         mean('balance_after').alias('balance_after_mean'),
                         min('balance_after').alias('balance_after_min'),
                         max('balance_after').alias('balance_after_max'),
                         count('correspondent_id').alias('txns'),
                         countDistinct('correspondent_id').alias('contacts'))
                    .groupby('name')
                    .pivot('txn_type')
                    .agg(first('amount_mean').alias('amount_mean'),
                         first('amount_min').alias('amount_min'),
                         first('amount_max').alias('amount_max'),
                         first('balance_before_mean').alias('balance_before_mean'),
                         first('balance_before_min').alias('balance_before_min'),
                         first('balance_before_max').alias('balance_before_max'),
                         first('balance_after_mean').alias('balance_after_mean'),
                         first('balance_after_min').alias('balance_after_min'),
                         first('balance_after_max').alias('balance_after_max'),
                         first('txns').alias('txns'),
                         first('contacts').alias('contacts')))
            # add df name to columns
            for col_name in aggs.columns[1:]:  # exclude 'name'
                aggs = aggs.withColumnRenamed(col_name, dfname + '_' + col_name)

            features.append(aggs)

        # Combine all mobile money features together and save them
        feats = long_join_pyspark(features, on='name', how='outer')
        feats = feats.toDF(*[c if c == 'name' else 'mobilemoney_' + c for c in feats.columns])
        save_df(feats, self.outputs + '/datasets/mobilemoney_feats.csv')
        self.features['mobilemoney'] = self.spark.read.csv(self.outputs + '/datasets/mobilemoney_feats.csv',
                                                           header=True, inferSchema=True)

    def recharges_features(self) -> None:

        if self.ds.recharges is None:
            raise ValueError('Recharges file must be loaded to calculate recharges features.')
        print('Calculating recharges features...')

        feats = self.ds.recharges.groupby('caller_id').agg(sum('amount').alias('sum'),
                                                           mean('amount').alias('mean'),
                                                           min('amount').alias('min'),
                                                           max('amount').alias('max'),
                                                           count('amount').alias('count'),
                                                           countDistinct('day').alias('days'))

        feats = feats.withColumnRenamed('caller_id', 'name')
        feats = feats.toDF(*[c if c == 'name' else 'recharges_' + c for c in feats.columns])
        save_df(feats, self.outputs + '/datasets/recharges_feats.csv')
        self.features['recharges'] = self.spark.read.csv(self.outputs + '/datasets/recharges_feats.csv',
                                                         header=True, inferSchema=True)

    def load_features(self) -> None:
        """
        Load features from disk if already computed
        """
        data_path = self.outputs + '/datasets/'

        features = ['cdr', 'cdr', 'international', 'location', 'mobiledata', 'mobilemoney', 'recharges']
        datasets = ['/bandicoot_features/all', 'cdr_features_spark/all', 'international_feats', 'location_features',
                    'mobiledata_features', 'mobilemoney_feats', 'recharges_feats']
        # Read data from disk if requested
        for feature, dataset in zip(features, datasets):
            if not self.features[feature]:
                try:
                    self.features[feature] = self.spark.read.csv(data_path + dataset + '.csv',
                                                                 header=True, inferSchema=True)
                except AnalysisException:
                    print(f"Could not locate or read data for '{dataset}'")

    def all_features(self, read_from_disk: bool = False) -> None:
        """
        Join all feature datasets together, save to disk, and assign to attribute

        Args:
            read_from_disk: whether to load features from disk
        """
        if read_from_disk:
            self.load_features()

        all_features_list = [self.features[key] for key in self.features.keys() if self.features[key] is not None]
        if all_features_list:
            all_features = long_join_pyspark(all_features_list, how='left', on='name')
            save_df(all_features, self.outputs + '/datasets/features.csv')
            self.features['all'] = self.spark.read.csv(self.outputs + '/datasets/features.csv',
                                                       header=True, inferSchema=True)
        else:
            print('No features have been computed yet.')

    def feature_plots(self, read_from_disk: bool = False) -> None:
        """
        Plot the distribution of a select number of features

        Args:
            read_from_disk: whether to load features from disk
        """
        if read_from_disk:
            self.load_features()

        # Plot of distributions of CDR features
        if self.features['cdr'] is not None:
            features = ['cdr_active_days__allweek__day__callandtext', 'cdr_call_duration__allweek__allday__call__mean',
                        'cdr_number_of_antennas__allweek__allday']
            names = ['Active Days', 'Mean Call Duration', 'Number of Antennas']
            distributions_plot(self.features['cdr'], features, names, color='indianred')
            plt.savefig(self.outputs + '/plots/cdr.png', dpi=300)
            plt.show()

        # Plot of distributions of international features
        if self.features['international'] is not None:
            features = ['international_all__recipient_id__count', 'international_all__recipient_id__nunique',
                        'international_call__duration__sum']
            names = ['International Transactions', 'International Contacts', 'Total International Call Time']
            distributions_plot(self.features['international'], features, names, color='darkorange')
            plt.savefig(self.outputs + '/plots/international.png', dpi=300)
            plt.show()

        # Plot of distributions of recharges features
        if self.features['recharges'] is not None:
            features = ['recharges_mean', 'recharges_count', 'recharges_days']
            names = ['Mean Recharge Amount', 'Number of Recharges', 'Number of Days with Recharges']
            distributions_plot(self.features['recharges'], features, names, color='mediumseagreen')
            plt.savefig(self.outputs + '/plots/recharges.png', dpi=300)
            plt.show()

        # Plot of distributions of mobile data features
        if self.features['mobiledata'] is not None:
            features = ['mobiledata_total_volume', 'mobiledata_mean_volume', 'mobiledata_num_days']
            names = ['Total Volume (MB)', 'Mean Transaction Volume (MB)', 'Number of Days with Data Usage']
            distributions_plot(self.features['mobiledata'], features, names, color='dodgerblue')
            plt.savefig(self.outputs + '/plots/mobiledata.png', dpi=300)
            plt.show()

        # Plot of distributions of mobile money features
        if self.features['mobilemoney'] is not None:
            features = ['mobilemoney_all_all_amount_mean', 'mobilemoney_all_all_balance_before_mean',
                        'mobilemoney_all_all_txns', 'mobilemoney_all_cashout_txns']
            names = ['Mean Amount', 'Mean Balance', 'Transactions', 'Cashout Transactions']
            distributions_plot(self.features['mobilemoney'], features, names, color='orchid')
            plt.savefig(self.outputs + '/plots/mobilemoney.png', dpi=300)
            plt.show()

        # Spatial plots
        if self.features['location'] is not None:
            for shapefile_name in self.ds.shapefiles.keys():
                fig, ax = plt.subplots(1, 1, figsize=(10, 10))
                columns = [c for c in self.features['location'].columns if
                           shapefile_name in c and 'percent' not in c and 'Unknown' not in c]
                counts = self.features['location'].select([sum(c) for c in columns]).toPandas()
                counts.columns = ['_'.join(c.split('_')[2:])[:-1] for c in counts.columns]
                counts = counts.T
                counts.columns = ['txn_count']
                counts['region'] = counts.index
                counts = self.ds.shapefiles[shapefile_name].merge(counts, on='region', how='left')
                counts['txn_count'] = counts['txn_count'].fillna(0) / counts['txn_count'].sum()
                counts.plot(ax=ax, column='txn_count', cmap='magma', legend=True, legend_kwds={'shrink': 0.5})
                ax.axis('off')
                ax.set_title('Proportion of Transactions by ' + shapefile_name, fontsize='large')
                plt.tight_layout()
                plt.savefig(self.outputs + '/plots/spatial_' + shapefile_name + '.png')
                plt.show()

        # Cuts by feature usage (mobile money, mobile data, international calls)
        if self.features['cdr'] is not None:

            all_subscribers = self.features['cdr'].select('name')

            if self.features['international'] is not None:
                international_subscribers: Optional[SparkDataFrame] = self.features['international'].where(
                    col('international_all__recipient_id__count') > 0).select('name')
            else:
                international_subscribers = None

            if self.features['mobiledata'] is not None:
                mobiledata_subscribers: Optional[SparkDataFrame] = self.features['mobiledata'].where(
                    col('mobiledata_num_transactions') > 0).select('name')
            else:
                mobiledata_subscribers = None

            if self.features['mobilemoney'] is not None:
                mobilemoney_subscribers: Optional[SparkDataFrame] = self.features['mobilemoney'].where(
                    col('mobilemoney_all_all_txns') > 0).select('name')
            else:
                mobilemoney_subscribers = None

            features = ['cdr_active_days__allweek__day__callandtext', 'cdr_call_duration__allweek__allday__call__mean',
                        'cdr_number_of_antennas__allweek__allday']
            names = ['Active Days', 'Mean Call Duration', 'Number of Antennas']

            fig, ax = plt.subplots(1, len(features), figsize=(20, 5))
            for a in range(len(features)):
                boxplot = []
                for subscribers, slice_name in [(all_subscribers, 'All'),
                                                (international_subscribers, 'I Callers'),
                                                (mobiledata_subscribers, 'MD Users'),
                                                (mobilemoney_subscribers, 'MM Users')]:
                    if subscribers is not None:
                        users = self.features['cdr'].join(subscribers, how='inner', on='name')
                        slice = users.select(['name', features[a]]).toPandas()
                        slice['slice_name'] = slice_name
                        boxplot.append(slice)
                boxplot_df = pd.concat(boxplot)
                boxplot_df[features[a]] = boxplot_df[features[a]].astype('float')
                sns.boxplot(data=boxplot_df, x=features[a], y='slice_name', ax=ax[a], palette="Set2", orient='h')
                ax[a].set_xlabel('Feature')
                ax[a].set_ylabel(names[a])
                ax[a].set_title(names[a], fontsize='large')
                clean_plot(ax[a])
            plt.savefig(self.outputs + '/plots/boxplots.png', dpi=300)
            plt.show()