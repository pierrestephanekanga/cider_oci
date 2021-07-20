from typing import ValuesView
from box import Box
import yaml
from helpers.utils import *
from helpers.plot_utils import *
from helpers.io_utils import *
from helpers.ml_utils import *
from sklearn.decomposition import PCA
from wpca import WPCA


class SurveyOutcomeGenerator:

    def __init__(self, cfg_dir, dataframe=None, clean_folders=False):

        # Read config file
        with open(cfg_dir, "r") as ymlfile:
            cfg = Box(yaml.safe_load(ymlfile))
        self.cfg = cfg
        data = cfg.path.survey.data
        outputs = cfg.path.survey.outputs
        self.outputs = outputs
        file_names = cfg.path.survey.file_names

        # Get hypeparameter grids
        self.grids = cfg.hyperparams
        for key1 in self.grids.keys():
            grid = {}
            for key2 in self.grids[key1].keys():
                if 'variance' not in key2 and 'missing' not in key2 and 'winsorizer' not in key2:
                    grid[key2] = self.grids[key1][key2]
            self.grids[key1] = grid

        # Initialize values
        if dataframe is not None:
            self.survey_data = dataframe
        else:
            self.survey_data = pd.read_csv(data + file_names.survey)
        if 'weight' not in self.survey_data.columns:
            self.survey_data['weight'] = 1

        # Get columns types
        self.continuous = cfg.col_types.survey.continuous
        self.categorical = cfg.col_types.survey.categorical
        self.binary = cfg.col_types.survey.binary

        # Prepare working directory
        make_dir(outputs, clean_folders)
    
    def asset_index(self, cols, use_weights=True):

        # Check that categorical/binary columns are not being included
        if len(set(cols).intersection(set(self.categorical))) > 0:
            print('Warning: %i columns are categorical but will be treated as continuous for the purpose of the asset index.' % 
                len(set(cols).intersection(set(self.categorical))))
        if len(set(cols).intersection(set(self.binary))):
            print('Warning: %i columns are binary but will be treated as continuous for the purpose of the asset index.' % 
                len(set(cols).intersection(set(self.binary)))) 

        # Drop observations with null values
        n_obs = len(self.survey_data)
        assets = self.survey_data.dropna(subset=cols)
        dropped = n_obs - len(assets)
        if  dropped > 0:
            print('Warning: Dropping %i observations with missing values (%i percent of all observations)' % (dropped, 100*dropped/n_obs))
        
        # Scale data
        scaler = MinMaxScaler()
        scaled_assets = scaler.fit_transform(assets[cols])
        
        # Calculate asset index and basis vector
        if use_weights:
            np.random.seed(2)
            pca = WPCA(n_components=1)
            w = np.vstack([assets['weight'].values for i in range(len(cols))]).T
            asset_index = pca.fit_transform(scaled_assets, weights=w)[:, 0]
        else:
            pca = PCA(n_components=1, random_state=1)
            asset_index = pca.fit_transform(scaled_assets)[:, 0]
        print('PCA variance explained: %.2f' % (100*pca.explained_variance_ratio_[0]) + '%')

        # Write asset index to file
        asset_index = pd.DataFrame([list(assets['unique_id']), asset_index]).T
        asset_index.columns = ['unique_id', 'asset_index']
        self.index = asset_index
        asset_index.to_csv(self.outputs + '/asset_index.csv', index=False)

        # Write basis vector to file
        basis_vector = pd.DataFrame([cols, pca.components_[0]]).T
        basis_vector.columns = ['Item', 'Magnitude']
        basis_vector = basis_vector.sort_values('Magnitude', ascending=False)
        self.basis_vector = basis_vector
        basis_vector.to_csv(self.outputs + '/basis_vector.csv', index=False)

        return asset_index

    def fit_pmt(self, outcome, cols, model_name='linear', kfold=5, use_weights=True, scale=False, winsorize=False):

        # Check that columns are typed correctly
        check_column_types(self.survey_data[cols], continuous=self.continuous, categorical=self.categorical, binary=self.binary)

        # Drop observations with null values
        data = self.survey_data[['unique_id', 'weight', outcome] + cols]
        n_obs = len(data)
        data = data.dropna(subset=[outcome] + list(set(cols).intersection(set(self.continuous + self.binary))))
        dropped = n_obs - len(data)
        if  dropped > 0:
            print('Warning: Dropping %i observations with missing values in continuous or binary columns or the outcome (%i percent of all observations)' % 
                (dropped, 100*dropped/n_obs))

        # Define preprocessing pipelines
        if scale and winsorize:
            continuous_transformer = Pipeline([('winsorizer', Winsorizer(limits=(.005, .995))), 
                                                ('scaler', StandardScaler())])
        elif winsorize:
            continuous_transformer = Pipeline([('winsorizer', Winsorizer(limits=(.005, .995)))])
        elif scale:
            continuous_transformer = Pipeline([('scaler', StandardScaler())])
        else:
            continuous_transformer = Pipeline([('null', 'passthrough')])
        categorical_transformer = OneHotEncoder(drop=None, handle_unknown='ignore')
        preprocessor = ColumnTransformer([('continuous', continuous_transformer, list(set(self.continuous).intersection(set(cols)))), 
                                        ('categorical', categorical_transformer, list(set(self.categorical).intersection(set(cols))))],
                                        sparse_threshold=0)

        # Compile model
        models = {
            'linear': LinearRegression(),
            'lasso': Lasso(),
            'ridge': Ridge(),
            'randomforest': RandomForestRegressor(random_state=1, n_jobs=-1),
            'gradientboosting': LGBMRegressor(random_state=1, n_jobs=-1, verbose=-10)
        }
        model = Pipeline([('preprocessor', preprocessor), ('model', models[model_name])])
        if model != 'linear':
            model = GridSearchCV(estimator=model,
                             param_grid=self.grids[model_name],
                             cv=kfold,
                             verbose=0,
                             scoring='r2',
                             refit='r2',
                             n_jobs=-1)


        # Fit and save model
        if use_weights:
            model.fit(data[cols], data[outcome], model__sample_weight=data['weight'])
        else:
             model.fit(data[cols], data[outcome])
        if model != 'linear':
            model = model.best_estimator_
        dump(model, self.outputs + '/' + model_name)

        # Save feature importances
        if 'feature_importances_' in dir(model.named_steps['model']):
            imports = model.named_steps['model'].feature_importances_
        else:
            imports = model.named_steps['model'].coef_

        colnames = list(pd.get_dummies(data[cols], columns=self.categorical, dummy_na=False, drop_first=False).columns)
        imports = pd.DataFrame([colnames, imports]).T
        imports.columns = ['Feature', 'Importance']
        imports = imports.sort_values('Importance', ascending=False)
        imports.to_csv(self.outputs + '/feature_importances_' + model_name + '.csv', index=False)

        # Get in sample and out of sample predictions
        insample = model.predict(data[cols])
        oos = cross_val_predict(model, data[cols], data[outcome], cv=kfold)
        predictions = pd.DataFrame([data['unique_id'].values, insample, oos]).T
        predictions.columns = ['unique_id', 'in_sample_prediction', 'out_of_sample_prediction']
        predictions = predictions.merge(data[['unique_id', 'weight', outcome] + cols], on='unique_id')
        predictions.to_csv(self.outputs + '/' + model_name + '_predictions.csv', index=False)
        if use_weights:
            r2 = r2_score(predictions[outcome], predictions['in_sample_prediction'], sample_weight=predictions['weight'])
        else:
            r2 = r2_score(predictions[outcome], predictions['in_sample_prediction'])
        print('R2 score: %.2f' % r2)
        return predictions


    def pretrained_pmt(self, other_data, cols, model_name, dataset_name='other_data'):

        # Load data
        if isinstance(other_data, str):
            other_data = pd.read_csv(other_data)

        # Check that all columns are present and check column types
        original_data = pd.read_csv(self.outputs + '/' + model_name + '_predictions.csv')
        check_columns_exist(original_data, cols, 'training dataset')
        check_columns_exist(other_data, cols, 'prediction dataset')
        check_column_types(other_data[cols], continuous=self.continuous, categorical=self.categorical, binary=self.binary)

        # Drop observations with null values
        other_data = other_data[['unique_id'] + cols]
        n_obs = len(other_data)
        other_data = other_data.dropna(subset=list(set(cols).intersection(set(self.continuous + self.binary))))
        dropped = n_obs - len(other_data)
        if  dropped > 0:
            print('Warning: Dropping %i observations with missing values in continuous or binary columns (%i percent of all observations)' % 
                (dropped, 100*dropped/n_obs))

        # Check that ranges are the same as training data
        for c in set(cols).intersection(set(self.categorical)):
            set_dif = set(other_data[c].dropna()).difference(set(original_data[c].dropna()))
            if len(set_dif) > 0:
                print('Warning: There are values in categorical column ' + c + \
                    ' that are not present in training data; they will not be positive for any dummy column. Values: ' + \
                    ','.join([str(x) for x in set_dif]))
        for c in set(cols).intersection(set(self.continuous)):
            if np.round(other_data[c].min(), 2) < np.round(original_data[c].min(), 2) or np.round(other_data[c].max(), 2) > np.round(original_data[c].max(), 2):
                print('Warning: There are values in continuous column ' + c + \
                    ' that are outside of the range in the training data; the original standardization will apply.')

        # Load and apply model, save predictions
        model = load(self.outputs + '/' + model_name)
        predictions = pd.DataFrame([other_data['unique_id'].values, model.predict(other_data[cols])]).T
        predictions.columns = ['unique_id', 'prediction']
        predictions = predictions.merge(other_data, on='unique_id')
        predictions.to_csv(self.outputs + '/' + model_name + '_predictions_' + dataset_name + '.csv', index=False)
        return predictions



    def select_features(self, cols, method='forward_selection', use_weights=True):

        # TODO
        return False





   