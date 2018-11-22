"""
Mixed Effects Random Forest

:copyright: 2017 Manifold, Inc.
:author: Sourav Dey <sdey@manifold.ai>
"""
import logging
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.exceptions import NotFittedError

logger = logging.getLogger(__name__)


class MERF(object):
    def __init__(self, n_estimators=300, min_iterations=10, gll_early_stop_threshold=None, max_iterations=20):
        self.n_estimators = n_estimators
        self.min_iterations = min_iterations
        self.gll_early_stop_threshold = gll_early_stop_threshold
        self.max_iterations = max_iterations

        self.cluster_counts = None
        self.trained_rf = None
        self.trained_b = None

        self.b_hat_history = []
        self.sigma2_hat_history = []
        self.D_hat_history = []
        self.gll_history = []

    def predict(self, X, Z, clusters):
        """
        Predict using trained MERF.  For known clusters the trained random effect correction is applied. For unknown
        clusters the pure fixed effect (RF) estimate is used.
        :param X: fixed effect covariates
        :param Z: random effect covariates
        :param clusters: cluster assignments for samples
        :return: y_hat, i.e. predictions
        """
        if self.trained_rf is None:
            raise NotFittedError(
                "This MERF instance is not fitted yet. Call 'fit' with appropriate arguments before "
                "using this method"
            )

        Z = np.array(Z)  # cast Z to numpy array (required if it's a dataframe, otw, the matrix mults later fail)

        # Apply random forest to all
        y_hat = self.trained_rf.predict(X)

        # Apply random effects correction to all known clusters. Note that then, by default, the new clusters get no
        # random effects correction -- which is the desired behavior.
        for cluster_id in self.cluster_counts.index:
            indices_i = clusters == cluster_id

            # If cluster doesn't exist in test data that's ok. Just move on.
            if len(indices_i) == 0:
                continue

            # If cluster does exist, apply the correction.
            b_i = self.trained_b.loc[cluster_id]
            Z_i = Z[indices_i]
            y_hat[indices_i] += Z_i.dot(b_i)

        return y_hat

    def fit(self, X, Z, clusters, y):
        """
        Fit MERF using EM algorithm.
        :param X: fixed effect covariates
        :param Z: random effect covariates
        :param clusters: cluster assignments for samples
        :param y: response/target variable
        :return: fitted model
        """

        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ Input Checks ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        assert len(Z) == len(X)
        assert len(y) == len(X)
        assert len(clusters) == len(X)

        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ Initialization ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        n_clusters = clusters.nunique()
        n_obs = len(y)
        q = Z.shape[1]  # random effects dimension
        Z = np.array(Z)  # cast Z to numpy array (required if it's a dataframe, otw, the matrix mults later fail)

        # Create a series where cluster_id is the index and n_i is the value
        cluster_counts = clusters.value_counts()

        # Do expensive slicing operations only once
        Z_by_cluster = {}
        y_by_cluster = {}
        n_by_cluster = {}
        I_by_cluster = {}
        indices_by_cluster = {}

        # TODO: Can these be replaced with groupbys? Groupbys are less understandable than brute force.
        for cluster_id in cluster_counts.index:
            # Find the index for all the samples from this cluster in the large vector
            indices_i = clusters == cluster_id
            indices_by_cluster[cluster_id] = indices_i

            # Slice those samples from Z and y
            Z_by_cluster[cluster_id] = Z[indices_i]
            y_by_cluster[cluster_id] = y[indices_i]

            # Get the counts for each cluster and create the appropriately sized identity matrix for later computations
            n_by_cluster[cluster_id] = cluster_counts[cluster_id]
            I_by_cluster[cluster_id] = np.eye(cluster_counts[cluster_id])

        # Intialize for EM algorithm
        iteration = 0
        # Note we are using a dataframe to hold the b_hat because this is easier to index into by cluster_id
        # Before we were using a simple numpy array -- but we were indexing into that wrong because the cluster_ids
        # are not necessarily in order.
        b_hat_df = pd.DataFrame(np.zeros((n_clusters, q)), index=cluster_counts.index)
        sigma2_hat = 1
        D_hat = np.eye(q)

        # vectors to hold history
        self.b_hat_history.append(b_hat_df)
        self.sigma2_hat_history.append(sigma2_hat)
        self.D_hat_history.append(D_hat)

        stop_flag, iter_begun = False, False

        while iteration < self.max_iterations and not stop_flag:
            iteration += 1
            logger.debug("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
            logger.debug("Iteration: {}".format(iteration))
            logger.debug("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")

            # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ E-step ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            # fill up y_star for all clusters
            y_star = np.zeros(len(y))
            for cluster_id in cluster_counts.index:
                # Get cached cluster slices
                y_i = y_by_cluster[cluster_id]
                Z_i = Z_by_cluster[cluster_id]
                b_hat_i = b_hat_df.loc[cluster_id]  # used to be ix
                logger.debug("E-step, cluster {}, b_hat = {}".format(cluster_id, b_hat_i))
                indices_i = indices_by_cluster[cluster_id]

                # Compute y_star for this cluster and put back in right place
                y_star_i = y_i - Z_i.dot(b_hat_i)
                y_star[indices_i] = y_star_i

            # check that still one dimensional
            # TODO: Other checks we want to do?
            assert len(y_star.shape) == 1

            # Do the random forest regression with all the fixed effects features
            rf = RandomForestRegressor(n_estimators=self.n_estimators, oob_score=True, n_jobs=-1)
            rf.fit(X, y_star)
            f_hat = rf.oob_prediction_

            # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ M-step ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
            sigma2_hat_sum = 0
            D_hat_sum = 0

            for cluster_id in cluster_counts.index:
                # Get cached cluster slices
                indices_i = indices_by_cluster[cluster_id]
                y_i = y_by_cluster[cluster_id]
                Z_i = Z_by_cluster[cluster_id]
                n_i = n_by_cluster[cluster_id]
                I_i = I_by_cluster[cluster_id]

                # index into f_hat
                f_hat_i = f_hat[indices_i]

                # Compute V_hat_i
                V_hat_i = Z_i.dot(D_hat).dot(Z_i.T) + sigma2_hat * I_i

                # Compute b_hat_i
                V_hat_inv_i = np.linalg.pinv(V_hat_i)
                logger.debug("M-step, pre-update, cluster {}, b_hat = {}".format(cluster_id, b_hat_df.loc[cluster_id]))
                b_hat_i = D_hat.dot(Z_i.T).dot(V_hat_inv_i).dot(y_i - f_hat_i)
                logger.debug("M-step, post-update, cluster {}, b_hat = {}".format(cluster_id, b_hat_i))

                # Compute the total error for this cluster
                eps_hat_i = y_i - f_hat_i - Z_i.dot(b_hat_i)

                logger.debug("------------------------------------------")
                logger.debug("M-step, cluster {}".format(cluster_id))
                logger.debug("error squared for cluster = {}".format(eps_hat_i.T.dot(eps_hat_i)))

                # Store b_hat for cluster both in numpy array and in dataframe
                # Note this HAS to be assigned with loc, otw whole df get erroneously assigned and things go to hell
                b_hat_df.loc[cluster_id, :] = b_hat_i
                logger.debug(
                    "M-step, post-update, recalled from db, cluster {}, "
                    "b_hat = {}".format(cluster_id, b_hat_df.loc[cluster_id])
                )

                # Update the sums for sigma2_hat and D_hat. We will update after the entire loop over clusters
                sigma2_hat_sum += eps_hat_i.T.dot(eps_hat_i) + sigma2_hat * (n_i - sigma2_hat * np.trace(V_hat_inv_i))
                D_hat_sum += np.outer(b_hat_i, b_hat_i) + (
                    D_hat - D_hat.dot(Z_i.T).dot(V_hat_inv_i).dot(Z_i).dot(D_hat)
                )  # noqa: E127

            # Normalize the sums to get sigma2_hat and D_hat
            sigma2_hat = (1.0 / n_obs) * sigma2_hat_sum
            D_hat = (1.0 / n_clusters) * D_hat_sum

            logger.debug("b_hat = {}".format(b_hat_df))
            logger.debug("sigma2_hat = {}".format(sigma2_hat))
            logger.debug("D_hat = {}".format(D_hat))

            # Store off history so that we can see the evolution of the EM algorithm
            self.b_hat_history.append(b_hat_df.copy())
            self.sigma2_hat_history.append(sigma2_hat)
            self.D_hat_history.append(D_hat)

            # Generalized Log Likelihood computation to check convergence
            gll = 0
            for cluster_id in cluster_counts.index:
                # Get cached cluster slices
                indices_i = indices_by_cluster[cluster_id]
                y_i = y_by_cluster[cluster_id]
                Z_i = Z_by_cluster[cluster_id]
                I_i = I_by_cluster[cluster_id]

                # Slice f_hat and get b_hat
                f_hat_i = f_hat[indices_i]
                R_hat_i = sigma2_hat * I_i
                b_hat_i = b_hat_df.loc[cluster_id]

                # Numerically stable way of computing log(det(A))
                _, logdet_D_hat = np.linalg.slogdet(D_hat)
                _, logdet_R_hat_i = np.linalg.slogdet(R_hat_i)

                gll += (
                    (y_i - f_hat_i - Z_i.dot(b_hat_i))
                    .T.dot(np.linalg.pinv(R_hat_i))
                    .dot(y_i - f_hat_i - Z_i.dot(b_hat_i))
                    + b_hat_i.T.dot(np.linalg.pinv(D_hat)).dot(b_hat_i)
                    + logdet_D_hat
                    + logdet_R_hat_i
                )  # noqa: E127

            logger.info("GLL is {} at iteration {}.".format(gll, iteration))
            self.gll_history.append(gll)

            if self.gll_early_stop_threshold is not None:
                if not iter_begun:
                    iter_begun = True
                else:
                    curr_threshold = np.abs((gll - self.gll_history[-2]) / self.gll_history[-2])
                    logger.debug("stop threshold = {}".format(curr_threshold))

                    if curr_threshold < self.gll_early_stop_threshold:
                        logger.info("Gll {} less than threshold {}, stopping early ...".
                                    format(gll, curr_threshold))
                        stop_flag = True

        # Store off most recent random forest model and b_hat as the model to be used in the prediction stage
        self.cluster_counts = cluster_counts
        self.trained_rf = rf
        self.trained_b = b_hat_df

        return self

    def score(self, X, Z, clusters, y):
        raise NotImplementedError()
