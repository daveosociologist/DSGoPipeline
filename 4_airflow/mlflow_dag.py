from datetime import datetime
import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.linear_model import LinearRegression
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score
import pandas as pd
import mlflow
import mlflow.sklearn
from mlflow.tracking.client import MlflowClient
import inspect
import os

from airflow import DAG
from airflow.operators.python_operator import PythonOperator


def eval_metrics(actual, pred):
    """ Youve seen this before """
    rmse = np.sqrt(mean_squared_error(actual, pred))
    mae = mean_absolute_error(actual, pred)
    r2 = r2_score(actual, pred)
    return rmse, mae, r2


def process_data(**kwargs):
    """ Task 1 - turn the raw data into a data product. """
    # Because I want to keep this simple and not connect a bucket or require you to download a large dataset, the process data
    # will simply load the *already* processed data, when ideally it should - you know - actually do the processing
    # To keep it in the MLflow framework, I am going to log the output data product
    with mlflow.start_run(run_name="process") as run:
        this_dir = os.path.abspath(os.path.dirname(inspect.stack()[0][1]))
        mlflow.log_artifact(os.path.join(this_dir, "germany.csv"), "processed_data")  # Log artifact in specific dir

        # Xcom is how tasks can send messages to each other
        kwargs["ti"].xcom_push(key="run_id", value=run.info.run_id)


def make_lr(**kwargs):
    """ Create a linear regression model and log it to mlflow """
    data_run_id = kwargs["ti"].xcom_pull(task_ids="process_data", key="run_id")
    client = MlflowClient()
    path = client.download_artifacts(data_run_id, "processed_data")  # Overkill in our case, but imagine they are on different servers, infrastructures

    df = pd.read_csv(path + "/germany.csv", parse_dates=[0], index_col=0)
    X = df[["windspeed", "temperature", "rad_horizontal", "rad_diffuse"]]
    y = df[["solar_GW", "wind_GW"]]
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    with mlflow.start_run(run_name="lr") as run:
        model = LinearRegression()
        model.fit(X_train, y_train)

        y_predict = model.predict(X_test)
        rmse, mae, r2 = eval_metrics(y_test, y_predict)

        mlflow.log_metric("rmse", rmse)  # New
        mlflow.log_metric("mae", mae)  # New
        mlflow.log_metric("r2", r2)  # New
        mlflow.sklearn.log_model(model, "model")  # New

        kwargs["ti"].xcom_push(key="run_id", value=[run.info.run_id])


def make_rf(**kwargs):
    """ Create a random forest model and log it to mlflow """
    data_run_id = kwargs["ti"].xcom_pull(task_ids="process_data", key="run_id")
    client = MlflowClient()
    path = client.download_artifacts(data_run_id, "processed_data")  # Overkill in our case, but imagine they are on different servers, infrastructures

    df = pd.read_csv(path + "/germany.csv", parse_dates=[0], index_col=0)

    X = df[["windspeed", "temperature", "rad_horizontal", "rad_diffuse"]]
    y = df[["solar_GW", "wind_GW"]]
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    runs = []
    for n_estimators in [4, 25]:
        for max_depth in [4, 10]:
            with mlflow.start_run(run_name="rf") as run:
                model = RandomForestRegressor(n_estimators=n_estimators, max_depth=max_depth)
                model.fit(X_train, y_train)

                y_predict = model.predict(X_test)
                rmse, mae, r2 = eval_metrics(y_test, y_predict)

                mlflow.log_param("n_estimators", n_estimators)  # New
                mlflow.log_param("max_depth", max_depth)  # New
                mlflow.log_metric("rmse", rmse)  # New
                mlflow.log_metric("mae", mae)  # New
                mlflow.log_metric("r2", r2)  # New
                mlflow.sklearn.log_model(model, "model")  # New
                runs.append(run.info.run_id)

    kwargs["ti"].xcom_push(key="run_id", value=runs)


def get_best_model(**kwargs):
    """ For all the models we logged, determine the best performing run """
    ids = [r for ids in kwargs["ti"].xcom_pull(task_ids=["model_lr", "model_rf"], key="run_id") for r in ids]
    client = MlflowClient()
    runs = [client.get_run(run_id) for run_id in ids]
    run_r2 = [run.data.metrics["r2"] for run in runs]
    best_run = runs[np.argmax(run_r2)]
    kwargs["ti"].xcom_push(key="best_model_run_id", value=best_run.info.run_id)


def register_best_model(**kwargs):
    """ Take the best performing model, register it under the BestModel name, and ship it to prod """
    run_id = kwargs["ti"].xcom_pull(task_ids="get_best_model", key="best_model_run_id")
    model_uri = f"runs:/{run_id}/model"
    model_details = mlflow.register_model(model_uri, "BestModel")  # note this doesnt put it in prod, but updates the registered model in the model repo

    # This is what would make it prod, but probably shouldnt automate this without some testing and eyes on
    client = MlflowClient()
    client.transition_model_version_stage(name=model_details.name, version=model_details.version, stage='Production')


mlflow.tracking.set_tracking_uri("http://127.0.0.1:5000")
mlflow.set_experiment("airflow")  # Notice, diff experiment name to keep things clear

# The airflow code is everything below here. We define the DAG in general, then its tasks, and how the tasks depend on each other
dag = DAG("DSGo",
          description="Lets turn our little project into a DAG that is set to run every single day at 6am",
          schedule_interval="0 6 * * *",
          start_date=datetime(2020, 6, 13),
          catchup=False)

# provide_context=True allows pushing and pulling variables
task_process = PythonOperator(task_id="process_data", python_callable=process_data, dag=dag, provide_context=True)
task_lr = PythonOperator(task_id="model_lr", python_callable=make_lr, dag=dag, provide_context=True)
task_rf = PythonOperator(task_id="model_rf", python_callable=make_rf, dag=dag, provide_context=True)
task_get_best_model = PythonOperator(task_id="get_best_model", python_callable=get_best_model, dag=dag, provide_context=True)
task_register_best_model = PythonOperator(task_id="register_best_model", python_callable=register_best_model, dag=dag, provide_context=True)

task_process >> task_lr  # So the linear regression needs to wait on the data processing
task_process >> task_rf
[task_lr, task_rf] >> task_get_best_model  # And the "best model" task waits on everything which makes models
task_get_best_model >> task_register_best_model  # You get it
